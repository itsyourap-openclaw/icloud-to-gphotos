"""The migration pipeline.

One run drains the iCloud library in bounded batches:

    collect -> download -> backfill metadata -> upload -> verify -> delete

Deletion is the only irreversible step, and it is gated on three independent
conditions, all of which must hold:

1. Every planned resource of the asset is recorded ``uploaded`` in the ledger,
   which only happens when gotohp reported a media key or a remote duplicate.
2. Capture and import are at least ``delete_grace_days`` old. When iCloud has no
   import date, first observation in the ledger starts that grace period.
3. ``delete_from_icloud`` is enabled and the run is not a dry run.

Assets are visited oldest-first. That ordering matters: the grace period
protects the newest items, so ascending order steadily drains the backlog
instead of re-examining photos that are too recent to touch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import albums as album_integration
from . import destination as destination_integration
from .assets import PlannedAsset, plan_asset, sanitize_stem
from .binaries import find_gotohp
from .config import Settings
from .downloader import download_batch, resource_path
from .icloud_client import ICloudSession
from .ledger import DestinationMismatch, Ledger
from .locking import MigrationBusy, migration_lock
from .metadata import MetadataReport, backfill_batch, find_exiftool
from .scan import ChangeScanner
from .uploader import FileVerdict, UploadError, UploadReport, upload_directory, verify_compatible

LOGGER = logging.getLogger(__name__)


@dataclass
class RunTotals:
    """Cumulative counters for one run."""

    scanned: int = 0
    planned: int = 0
    downloaded: int = 0
    bytes_downloaded: int = 0
    uploaded: int = 0
    failed: int = 0
    purged_assets: int = 0
    purge_failures: int = 0
    skipped_recent: int = 0
    already_uploaded: int = 0

    def as_dict(self) -> dict[str, int]:
        """Serialise for the JSON run report."""
        return {
            "scanned": self.scanned,
            "planned": self.planned,
            "downloaded": self.downloaded,
            "bytes_downloaded": self.bytes_downloaded,
            "uploaded": self.uploaded,
            "failed": self.failed,
            "purged_assets": self.purged_assets,
            "purge_failures": self.purge_failures,
            "skipped_recent": self.skipped_recent,
            "already_uploaded": self.already_uploaded,
        }


@dataclass
class RunResult:
    """Everything a run produced, ready to serialise into a report."""

    run_id: str
    status: str = "ok"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_seconds: float = 0.0
    batches: int = 0
    dry_run: bool = False
    totals: RunTotals = field(default_factory=RunTotals)
    metadata: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    library_remaining: int | None = None
    would_delete: list[str] = field(default_factory=list)
    scan: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the JSON run report."""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 1),
            "batches": self.batches,
            "dry_run": self.dry_run,
            "totals": self.totals.as_dict(),
            "metadata": self.metadata,
            "uploads": self.uploads,
            "errors": self.errors,
            "blocked": self.blocked,
            "library_remaining": self.library_remaining,
            "would_delete": self.would_delete[:200],
            "scan": self.scan,
        }


@dataclass(slots=True)
class _Batch:
    """A bounded unit of work: assets to download plus assets ready to delete."""

    to_download: list[PlannedAsset] = field(default_factory=list)
    ready_to_purge: list[PlannedAsset] = field(default_factory=list)
    planned_bytes: int = 0

    def __bool__(self) -> bool:
        return bool(self.to_download or self.ready_to_purge)


class Pipeline:
    """Coordinates one full migration run."""

    def __init__(
        self,
        settings: Settings,
        session: ICloudSession,
        ledger: Ledger,
        *,
        dry_run: bool = False,
    ) -> None:
        self.settings = settings
        self.session = session
        self.ledger = ledger
        from .archive import MetadataArchive

        self._metadata_archive: MetadataArchive | None = None
        self.dry_run = dry_run
        self.exiftool = find_exiftool(settings.exiftool_binary)
        self.gotohp = find_gotohp(settings.gotohp_binary)
        self._scanner: ChangeScanner | None = None
        self._staging = {
            "media": settings.media_staging_dir,
            "edited": settings.edited_staging_dir,
        }
        self._last_upload = UploadReport()
        self._album_sync: album_integration.AlbumSync | None = None

    # --- Public entry point -------------------------------------------------

    def run(self, run_id: str) -> RunResult:
        """Execute a full run and return its result."""
        try:
            with migration_lock(self.settings):
                return self._run_locked(run_id)
        except MigrationBusy as exc:
            return RunResult(run_id=run_id, dry_run=self.dry_run, status="error", errors=[str(exc)])

    def _run_locked(self, run_id: str) -> RunResult:
        """Run while the caller holds both locks (also used by the CLI)."""
        if self.dry_run:
            original_ledger = self.ledger
            with original_ledger.snapshot() as preview:
                self.ledger = preview
                try:
                    return self._execute(run_id)
                finally:
                    self.ledger = original_ledger
        assert self.settings.staging_dir is not None
        previous_staging = self._staging
        with tempfile.TemporaryDirectory(prefix="run-", dir=self.settings.staging_dir) as staging:
            self._staging = {"media": Path(staging) / "media", "edited": Path(staging) / "edited"}
            try:
                return self._execute(run_id)
            finally:
                self._staging = previous_staging

    def _execute(self, run_id: str) -> RunResult:
        result = RunResult(run_id=run_id, dry_run=self.dry_run)
        self._destination: destination_integration.DestinationGuard | None = None
        self._destination_reconcile = False
        self._repair_epoch = result.started_at.isoformat()
        self._pending_asset: Any = None
        self._album_sync = None
        started = time.monotonic()

        if self.gotohp is None:
            result.status = "error"
            result.errors.append(
                "gotohp CLI not found. Set I2G_GOTOHP_BINARY or place it in ./bin. "
                "See docs/SETUP.md."
            )
            result.duration_seconds = time.monotonic() - started
            return result

        # Checked before any download: an unusable gotohp would otherwise only
        # surface after a whole batch has been fetched, wasting the bandwidth
        # and leaving every file recorded as failed.
        if not self.dry_run:
            try:
                if self.settings.update_existing_photos_to_live:
                    verify_compatible(self.gotohp, update_existing_photos_to_live=True)
                else:
                    verify_compatible(self.gotohp)
                self._destination = destination_integration.DestinationGuard(
                    self.gotohp, self.settings.gotohp_config)
                self._destination_reconcile = self.ledger.bind_destination(
                    self._destination.identity)
            except (UploadError, DestinationMismatch) as exc:
                result.status = "error"
                result.errors.append(str(exc))
                LOGGER.error("%s", exc)
                result.duration_seconds = time.monotonic() - started
                return result

        if self.exiftool is None and self.settings.backfill_metadata:
            LOGGER.warning(
                "exiftool not found; HEIC and video capture dates cannot be verified "
                "or repaired. See docs/SETUP.md."
            )

        self._metadata_archive = None
        self._clear_staging()
        assets = self._source_assets()

        try:
            while True:
                if self.settings.max_batches_per_run is not None and (
                    result.batches >= self.settings.max_batches_per_run
                ):
                    LOGGER.info("Reached max_batches_per_run=%s", self.settings.max_batches_per_run)
                    result.status = "partial"
                    break

                budget = self._byte_budget()
                if budget <= 0:
                    message = (
                        "Free disk is at or below the configured headroom of "
                        f"{_human(self.settings.disk_headroom_bytes)}; stopping early."
                    )
                    LOGGER.error(message)
                    result.errors.append(message)
                    result.status = "partial"
                    break

                batch = self._collect_batch(assets, result, budget)
                if not batch:
                    break

                result.batches += 1
                LOGGER.info(
                    "Batch %d: %d asset(s) to download (~%s), %d ready to delete",
                    result.batches,
                    len(batch.to_download),
                    _human(batch.planned_bytes),
                    len(batch.ready_to_purge),
                )
                self._process_batch(batch, result)
                self._settle_scan(batch, result)
                self._clear_staging()
        except KeyboardInterrupt:
            result.status = "interrupted"
            result.errors.append("Run interrupted by operator.")
            LOGGER.warning("Interrupted; state is preserved in the ledger.")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            result.status = "error"
            result.errors.append(f"{type(exc).__name__}: {exc}")
            LOGGER.exception("Run failed")

        assets.close()
        if self._scanner is not None:
            result.scan = self._scanner.summary()
            if self._scanner.lookup_failures:
                result.errors.append(
                    f"{self._scanner.lookup_failures} asset lookups deferred for retry."
                )
            self._scanner.close()
            self._scanner = None

        result.blocked = [
            {
                "asset_id": row.asset_id,
                "resource": row.resource_key,
                "file": row.filename,
                "attempts": row.attempts,
                "error": row.error,
            }
            for row in self.ledger.blocked_resources()
        ]
        if result.blocked and result.status == "ok":
            result.status = "ok_with_blocked"
        if result.status == "ok" and (
            result.errors or result.totals.failed or result.totals.purge_failures
            or any(report.get("failed") or report.get("exit_code") for report in result.uploads)
        ):
            result.status = "partial"

        try:
            result.library_remaining = self.session.library_size()
        except Exception as exc:  # noqa: BLE001 - informational only
            LOGGER.debug("Could not read library size: %s", exc)

        result.duration_seconds = time.monotonic() - started
        if self._album_sync is not None:
            self._album_sync.close()
            self._album_sync = None
        return result

    # --- Batch collection ---------------------------------------------------

    def _source_assets(self) -> Generator[Any, None, None]:
        filtered = bool(
            getattr(self.settings, "include_albums", [])
            or getattr(self.settings, "exclude_albums", [])
        )
        if self.settings.incremental_scan and not self.dry_run and not filtered:
            self._scanner = ChangeScanner(self.settings, self.session, self.ledger)
            if self._destination_reconcile:
                self._scanner.require_full_scan()
            yield from self._scanner.assets()
        else:
            # Filtered traversals must use the session's selectors, never all.get().
            yield from self.session.iter_all_assets()
        if self._destination_reconcile and not filtered:
            self.ledger.finish_destination_reconciliation()

    def _settle_scan(self, batch: _Batch, result: RunResult) -> None:
        if self._scanner is None:
            return
        for planned in batch.to_download + batch.ready_to_purge:
            row = self.ledger.get_asset(planned.asset_id)
            purged = row is not None and row.purged_at is not None
            backed_up = (
                not self.settings.delete_from_icloud and not planned.preservation_errors
                and not result.errors and not result.totals.failed
                and self.ledger.asset_ready_to_purge(planned.asset_id)
            )
            if purged or backed_up:
                self._scanner.store.acknowledge(planned.asset_id)

    def _collect_batch(
        self, assets: Iterator[Any], result: RunResult, byte_budget: int
    ) -> _Batch:
        """Pull assets off the library iterator until a batch budget is reached.

        The iterator is shared across batches for the whole run, so each batch
        continues where the last stopped and the run terminates when the library
        is exhausted.
        """
        batch = _Batch()
        now = datetime.now(UTC)
        disk_budget = self._available_disk_bytes()

        while True:
            if self._pending_asset is not None:
                asset, self._pending_asset = self._pending_asset, None
            else:
                try:
                    asset = next(assets)
                except StopIteration:
                    break
                result.totals.scanned += 1
            try:
                planned = self._plan(asset)
            except Exception as exc:  # noqa: BLE001 - one bad asset must not stop the run
                LOGGER.warning("Could not plan asset %s: %s", getattr(asset, "id", "?"), exc)
                result.errors.append(f"plan failed for {getattr(asset, 'id', '?')}: {exc}")
                continue

            for error in planned.preservation_errors:
                LOGGER.warning("%s: %s", planned.asset_id, error)
                result.errors.append(f"{planned.asset_id}: {error}")
                result.status = "partial"
            if not planned.resources:
                continue

            self._register(planned)
            rows = {row.resource_key: row for row in self.ledger.get_resources(planned.asset_id)}
            outstanding = [
                res
                for res in planned.resources
                if not (row := rows.get(res.key))
                or (not row.is_uploaded and not row.is_exhausted)
            ]
            manager = self._album_manager()
            if manager is not None and manager.pending(planned) and not any(
                row.is_exhausted for row in rows.values()
            ):
                outstanding = list(planned.resources)

            if outstanding:
                required = sum(max(res.size or 0, 0) for res in outstanding)
                if required > disk_budget:
                    result.errors.append(
                        f"{planned.asset_id}: needs {_human(required)}, but only "
                        f"{_human(max(disk_budget, 0))} is available above disk headroom; deferred."
                    )
                    result.status = "partial"
                    continue
                if batch.planned_bytes + required > disk_budget:
                    self._pending_asset = asset
                    break
                batch.to_download.append(planned)
                batch.planned_bytes += required
                result.totals.planned += len(outstanding)
            elif self.ledger.asset_ready_to_purge(planned.asset_id):
                # Uploaded on an earlier run but not yet deletable then.
                result.totals.already_uploaded += 1
                if self._purge_allowed(planned, now):
                    batch.ready_to_purge.append(planned)
                elif not planned.preservation_errors:
                    result.totals.skipped_recent += 1
            elif self.ledger.asset_blocked(planned.asset_id):
                LOGGER.debug("Asset %s is blocked by exhausted retries.", planned.asset_id)

            if (
                len(batch.to_download) >= self.settings.batch_max_items
                or batch.planned_bytes >= byte_budget
            ):
                break

        return batch

    def _plan(self, asset: Any) -> PlannedAsset:
        from .archive import MetadataArchive

        preferred = sanitize_stem(Path(asset.filename).stem)
        stem = self.ledger.reserve_stem(asset.id, preferred)
        planned = plan_asset(asset, self.settings, stem)
        if self.settings.metadata_archive_dir is not None and not self.dry_run:
            try:
                if self._metadata_archive is None:
                    self._metadata_archive = MetadataArchive(
                        self.settings.metadata_archive_dir,
                        self.settings.icloud_username,
                        self.session.library,
                    )
                self._metadata_archive.write(planned)
            except Exception as exc:
                planned.preservation_errors.append(f"metadata archive failed: {exc}")
        return planned

    def _register(self, planned: PlannedAsset) -> None:
        with self.ledger.transaction():
            self.ledger.upsert_asset(
                asset_id=planned.asset_id,
                master_id=planned.master_id,
                filename=planned.filename,
                stem=planned.stem,
                item_type=planned.item_type,
                asset_date=planned.asset_date,
                added_date=planned.added_date,
                is_live_photo=planned.is_live_photo,
                has_adjustments=planned.has_adjustments,
            )
            for res in planned.resources:
                self.ledger.upsert_resource(
                    asset_id=planned.asset_id,
                    resource_key=res.key,
                    filename=res.filename,
                    staging_root=res.staging_root,
                    size=res.size,
                    checksum=res.resource.checksum,
                )
            if self.settings.update_existing_photos_to_live and planned.is_live_photo:
                self.ledger.prepare_live_photo_pair(planned.asset_id, self._pair_signature(planned))

    # --- Batch processing ---------------------------------------------------

    def _process_batch(self, batch: _Batch, result: RunResult) -> None:
        if batch.to_download:
            self._download(batch, result)
            self._backfill(batch, result)
            self._upload(result)
            self._verify(batch, result)
            self._preserve_albums(batch, result)

        self._purge(batch, result)

    def _download(self, batch: _Batch, result: RunResult) -> None:
        if self.dry_run:
            LOGGER.info("[dry-run] would download %d asset(s)", len(batch.to_download))
            return

        # Resolved here, on the main thread: the ledger's SQLite connection
        # cannot be read from the download workers.
        skip_keys = frozenset(
            (planned.asset_id, row.resource_key)
            for planned in batch.to_download
            for row in self.ledger.get_resources(planned.asset_id)
            if row.is_exhausted or (row.is_uploaded and not (
                self._album_sync is not None and self._album_sync.pending(planned)
            ))
        )

        outcome = download_batch(
            batch.to_download,
            self._staging,
            workers=self.settings.download_workers,
            skip_keys=skip_keys,
            disk_headroom_bytes=self.settings.disk_headroom_bytes,
            reservation_factor=self._disk_reservation_factor(),
        )
        with self.ledger.transaction():
            for item in outcome.files:
                prior = self.ledger.get_resource(item.asset_id, item.resource_key)
                if prior is None or not prior.is_uploaded:
                    self.ledger.mark_downloaded(item.asset_id, item.resource_key, item.size)
            for failure in outcome.failures:
                prior = self.ledger.get_resource(failure.asset_id, failure.resource_key)
                if prior is not None and prior.is_uploaded:
                    continue  # Album materialization failure must not erase upload evidence.
                mark = (
                    self.ledger.mark_deferred if failure.capacity_limited
                    else self.ledger.mark_failed
                )
                mark(
                    failure.asset_id, failure.resource_key, f"download: {failure.error}"
                )

        result.totals.downloaded += len(outcome.files)
        result.totals.bytes_downloaded += outcome.bytes_written
        result.totals.failed += len(outcome.failures)
        LOGGER.info(
            "Downloaded %d file(s), %s; %d failure(s)",
            len(outcome.files),
            _human(outcome.bytes_written),
            len(outcome.failures),
        )

    def _backfill(self, batch: _Batch, result: RunResult) -> None:
        if self.dry_run:
            return
        items: list[tuple[PlannedAsset, Path]] = []
        for planned in batch.to_download:
            for res in planned.resources:
                path = resource_path(self._staging, res)
                if path.exists():
                    items.append((planned, path))
        if not items:
            return
        report: MetadataReport = backfill_batch(
            items, exiftool=self.exiftool, enabled=self.settings.backfill_metadata
        )
        LOGGER.info(
            "Metadata: %d date(s) and %d GPS tag(s) backfilled across %d file(s)",
            report.dates_written,
            report.gps_written,
            report.files_examined,
        )
        result.metadata.append(report.as_dict())
        result.errors.extend(report.errors[:10])

    def _upload(self, result: RunResult) -> None:
        if self.dry_run:
            LOGGER.info("[dry-run] would upload staged media")
            return

        combined = UploadReport()
        # Two passes: originals and Live Photo components need pairing, edited
        # renders must not be paired (they share a stem with their still).
        passes = (
            (self._staging["media"], self.settings.pair_live_photos),
            (self._staging["edited"], False),
        )
        assert self._destination is not None
        for directory, pair in passes:
            self._destination.check()
            try:
                report = upload_directory(
                    directory,
                    binary=self.gotohp,  # type: ignore[arg-type]
                    threads=self.settings.upload_threads,
                    pair_live_photos=pair,
                    update_existing_photos_to_live=(
                        pair and self.settings.update_existing_photos_to_live
                    ),
                    ignore_apple_metadata=self.settings.ignore_apple_metadata,
                    config_path=self.settings.gotohp_config,
                )
            except UploadError as exc:
                LOGGER.error("Upload pass for %s failed: %s", directory.name, exc)
                result.errors.append(f"upload({directory.name}): {exc}")
                continue
            self._destination.check()
            combined.merge(report)

        result.uploads.append(combined.as_dict())
        self._last_upload = combined

    def _verify(self, batch: _Batch, result: RunResult) -> None:
        """Reconcile gotohp's verdicts back onto ledger rows."""
        if self.dry_run:
            return

        combined: UploadReport = getattr(self, "_last_upload", UploadReport())
        by_path: dict[str, list[FileVerdict]] = {}
        for reported in combined.verdicts:
            if reported.path is not None:
                by_path.setdefault(reported.path, []).append(reported)
        now = datetime.now(UTC)

        with self.ledger.transaction():
            for planned in batch.to_download:
                repair = self.settings.update_existing_photos_to_live and planned.is_live_photo
                pair_paths = {
                    str(resource_path(self._staging, res).resolve())
                    for res in planned.resources if res.key in ("original", "original_video")
                }
                pair_verdicts = [by_path.get(path, []) for path in pair_paths]
                linked = (
                    len(pair_paths) == 2
                    and all(Path(path).is_file() for path in pair_paths)
                    and all(len(matches) == 1 for matches in pair_verdicts)
                    and all(
                        matches[0].uploaded and matches[0].media_key
                        and set(matches[0].related_paths) == pair_paths
                        for matches in pair_verdicts
                    )
                    and len({matches[0].media_key for matches in pair_verdicts}) == 1
                )
                for res in planned.resources:
                    row = self.ledger.get_resource(planned.asset_id, res.key)
                    if row is not None and row.is_uploaded:
                        continue
                    path = resource_path(self._staging, res)
                    matches = by_path.get(str(path.resolve()), [])
                    verdict = matches[0] if len(matches) == 1 else None
                    if (
                        repair and res.key in ("original", "original_video") and not linked
                    ):
                        verdict = None
                    if (
                        row is not None and row.state == "downloaded" and path.is_file()
                        and verdict is not None and verdict.uploaded
                    ):
                        self.ledger.mark_uploaded(
                            planned.asset_id,
                            res.key,
                            verdict.media_key,
                        )
                        result.totals.uploaded += 1
                    elif row is not None and row.state == "downloaded":
                        reason = (
                            verdict.reason
                            if verdict and verdict.reason
                            else "not reported by gotohp"
                        )
                        self.ledger.mark_failed(
                            planned.asset_id, res.key, f"upload: {reason}"
                        )
                        result.totals.failed += 1
                        LOGGER.warning(
                            "Not confirmed remote: %s (%s)", res.filename, reason
                        )
                    elif not path.exists() and row is not None and row.state == "failed":
                        # Download already failed; nothing further to record.
                        pass
                if repair and linked:
                    self.ledger.confirm_live_photo_pair(
                        planned.asset_id, self._pair_signature(planned)
                    )

        for planned in batch.to_download:
            if self.ledger.asset_ready_to_purge(planned.asset_id):
                if self._purge_allowed(planned, now):
                    batch.ready_to_purge.append(planned)
                elif not planned.preservation_errors:
                    result.totals.skipped_recent += 1

    def _album_manager(self) -> album_integration.AlbumSync | None:
        if self.settings.preserve_albums and not self.dry_run and self._album_sync is None:
            assert self.gotohp is not None
            self._album_sync = album_integration.AlbumSync(
                self.settings, self.session.library, self.ledger, self.gotohp,
                guard=self._destination,
            )
        return self._album_sync

    def _preserve_albums(self, batch: _Batch, result: RunResult) -> None:
        manager = self._album_manager()
        if manager is None:
            return
        for planned in batch.to_download:
            if not self.ledger.asset_ready_to_purge(planned.asset_id):
                continue
            try:
                manager.sync(planned, self._staging)
            except Exception as exc:
                result.errors.append(f"albums({planned.asset_id}): {exc}")
                result.status = "partial"
                continue
            if (
                self._purge_allowed(planned, datetime.now(UTC))
                and planned not in batch.ready_to_purge
            ):
                batch.ready_to_purge.append(planned)

    # --- Deletion -----------------------------------------------------------

    def _pair_signature(self, planned: PlannedAsset) -> str:
        pair = [r for r in planned.resources if r.key in ("original", "original_video")]
        fingerprint = [(r.key, r.resource.checksum, r.size) for r in pair]
        # Without content fingerprints, do not reuse a previous run's linkage proof.
        fingerprinted = len(pair) == 2 and all(r.resource.checksum for r in pair)
        epoch = "" if fingerprinted else self._repair_epoch
        return hashlib.sha256(json.dumps([fingerprint, epoch]).encode()).hexdigest()

    def _purge_allowed(self, planned: PlannedAsset, now: datetime) -> bool:
        """Require complete preservation as well as the age grace period."""
        if self.settings.icloud_library not in (None, "root"):
            return False
        recorded = self.ledger.get_asset(planned.asset_id)
        if recorded is None:
            return False
        if (
            self.settings.update_existing_photos_to_live and planned.is_live_photo
            and not self.ledger.live_photo_pair_confirmed(
                planned.asset_id, self._pair_signature(planned)
            )
        ):
            return False
        return (
            (self._album_sync is None or not self._album_sync.pending(planned))
            and not planned.preservation_errors
            and planned.preservation_age_days(datetime.fromisoformat(recorded.first_seen_at), now)
            >= self.settings.delete_grace_days
        )

    def _purge(self, batch: _Batch, result: RunResult) -> None:
        """Delete verified assets from iCloud, then their local copies."""
        if not batch.ready_to_purge:
            return

        if not self.settings.delete_from_icloud or self.dry_run:
            label = "dry-run" if self.dry_run else "deletion disabled"
            LOGGER.info(
                "[%s] %d asset(s) verified in Google Photos and eligible for deletion",
                label,
                len(batch.ready_to_purge),
            )
            result.would_delete.extend(
                f"{p.asset_id} ({p.filename})" for p in batch.ready_to_purge
            )
            return

        assert self._destination is not None
        self._destination.check()
        for planned in batch.ready_to_purge:
            if not self._purge_allowed(planned, datetime.now(UTC)):
                continue
            # Re-check against the ledger rather than trusting the batch list;
            # this is the last gate before an irreversible action.
            if not self.ledger.asset_ready_to_purge(planned.asset_id):
                LOGGER.error(
                    "Refusing to delete %s: ledger no longer reports it fully uploaded.",
                    planned.asset_id,
                )
                continue
            try:
                deleted = self.session.delete_asset(planned.asset)
            except Exception as exc:  # noqa: BLE001 - per-asset failure is recoverable
                LOGGER.warning("iCloud delete failed for %s: %s", planned.asset_id, exc)
                self.ledger.mark_asset_purge_failed(planned.asset_id, str(exc))
                result.totals.purge_failures += 1
                continue

            if not deleted:
                LOGGER.warning("iCloud reported no deletion for %s", planned.asset_id)
                self.ledger.mark_asset_purge_failed(planned.asset_id, "delete returned false")
                result.totals.purge_failures += 1
                continue

            self.ledger.mark_asset_purged(planned.asset_id)
            result.totals.purged_assets += 1
            self._remove_local(planned)

        LOGGER.info(
            "Deleted %d asset(s) from iCloud (%d failure(s))",
            result.totals.purged_assets,
            result.totals.purge_failures,
        )

    def _remove_local(self, planned: PlannedAsset) -> None:
        for res in planned.resources:
            path = resource_path(self._staging, res)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # noqa: PERF203 - staging is wiped anyway
                LOGGER.debug("Could not remove %s: %s", path, exc)

    # --- Housekeeping -------------------------------------------------------

    def _clear_staging(self) -> None:
        """Empty the staging directories between batches."""
        if self.dry_run:
            return
        for directory in self._staging.values():
            if directory.exists():
                shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True, exist_ok=True)

    def _byte_budget(self) -> int:
        """Bytes this batch may download.

        The configured cap, shrunk to whatever the filesystem can actually spare
        above the headroom. Returns 0 or less when there is no room to work.
        """
        return min(self.settings.batch_max_bytes, self._available_disk_bytes())

    def _available_disk_bytes(self) -> int:
        """Hard disk limit, independent of the configured soft batch cap."""
        assert self.settings.staging_dir is not None
        try:
            free = shutil.disk_usage(self.settings.staging_dir).free
        except OSError:
            LOGGER.error("Cannot determine free space; refusing downloads.", exc_info=True)
            return 0
        return (free - self.settings.disk_headroom_bytes) // self._disk_reservation_factor()

    def _disk_reservation_factor(self) -> int:
        # ExifTool may temporarily rewrite a complete file. Leave room for a
        # second copy of the batch while repairing metadata.
        return 2 if self.settings.backfill_metadata and self.exiftool is not None else 1


def _human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"
