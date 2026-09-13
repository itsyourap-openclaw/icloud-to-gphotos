"""Streaming downloads of iCloud photo resources.

pyicloud's ``PhotoAsset.download`` reads the whole response into memory, which is
untenable for multi-gigabyte videos. We stream to a temporary file in the target
directory and atomically rename on success, so a crashed or killed run never
leaves a truncated file that looks complete.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import BinaryIO

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .assets import PlannedAsset, PlannedResource

LOGGER = logging.getLogger(__name__)

CHUNK_BYTES = 1024 * 1024


class DownloadError(RuntimeError):
    """A resource could not be downloaded."""


class DiskSpaceError(DownloadError):
    """The volume cannot accommodate more bytes above the required headroom."""


class DiskBudget:
    """Shared byte reservation and live-space checks for concurrent streams."""

    def __init__(self, directory: Path, headroom: int, reservation_factor: int = 1) -> None:
        self.directory = directory
        self.headroom = headroom
        self.factor = reservation_factor
        self._lock = Lock()
        self._remaining = max(0, self._free() // self.factor)

    def _free(self) -> int:
        try:
            return shutil.disk_usage(self.directory).free - self.headroom
        except OSError as exc:
            raise DiskSpaceError("Cannot determine free disk space.") from exc

    def write(self, handle: BinaryIO, chunk: bytes) -> None:
        """Serialize capacity checks and writes so workers cannot oversubscribe."""
        with self._lock:
            if len(chunk) > self._remaining or len(chunk) * self.factor > self._free():
                raise DiskSpaceError("Insufficient disk space above headroom; download deferred.")
            self._remaining -= len(chunk)
            handle.write(chunk)
            handle.flush()

    def release(self, size: int) -> None:
        """Return the reservation after removing an incomplete download."""
        with self._lock:
            self._remaining += size


@dataclass(slots=True)
class DownloadedFile:
    """A resource successfully written to local disk."""

    asset_id: str
    resource_key: str
    path: Path
    size: int


@dataclass(slots=True)
class DownloadFailure:
    """A resource that could not be written to local disk."""

    asset_id: str
    resource_key: str
    error: str
    capacity_limited: bool = False


@dataclass(slots=True)
class DownloadOutcome:
    """The result of downloading one batch."""

    files: list[DownloadedFile]
    failures: list[DownloadFailure]

    @property
    def bytes_written(self) -> int:
        """Total bytes written to disk."""
        return sum(item.size for item in self.files)


@retry(
    retry=retry_if_exception(
        lambda exc: (
            isinstance(exc, (OSError, DownloadError)) and not isinstance(exc, DiskSpaceError)
        )
    ),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def _stream_to_disk(
    session: object, url: str, target: Path, expected_size: int | None,
    disk_budget: DiskBudget | None = None,
) -> int:
    """Stream ``url`` into ``target`` atomically, returning the byte count."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.part")
    written = 0
    response = None
    try:
        response = session.get(url, stream=True, timeout=(30, 300))  # type: ignore[attr-defined]
        response.raise_for_status()
        with temp.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                if chunk:
                    if disk_budget is None:
                        handle.write(chunk)
                    else:
                        disk_budget.write(handle, chunk)
                    written += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_size is not None and written != expected_size:
            raise DownloadError(
                f"size mismatch for {target.name}: expected {expected_size}, got {written}"
            )
        if written == 0:
            raise DownloadError(f"empty response for {target.name}")
        temp.replace(target)
    except Exception as exc:
        temp.unlink(missing_ok=True)
        if disk_budget is not None:
            disk_budget.release(written)
        if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise DiskSpaceError("Filesystem space or quota exhausted; download deferred.") from exc
        if isinstance(exc, (OSError, DownloadError)):
            raise
        raise DownloadError(f"{type(exc).__name__}: {exc}") from exc

    finally:
        if response is not None:
            response.close()
    return written


def resource_path(staging_dirs: dict[str, Path], resource: PlannedResource) -> Path:
    """Resolve the on-disk path for a planned resource."""
    return staging_dirs[resource.staging_root] / resource.filename


def download_asset(
    planned: PlannedAsset,
    staging_dirs: dict[str, Path],
    *,
    skip_keys: frozenset[tuple[str, str]] = frozenset(),
    disk_budget: DiskBudget | None = None,
) -> tuple[list[DownloadedFile], list[DownloadFailure]]:
    """Download every planned resource of one asset.

    Args:
        planned: The asset and its resource plan.
        staging_dirs: Mapping of staging root name to directory.
        skip_keys: ``(asset_id, resource_key)`` pairs already handled. Passed as
            a plain set rather than a callback because this runs on worker
            threads, where touching the ledger's SQLite connection would fail.
    """
    session = planned.asset._service.session  # noqa: SLF001
    files: list[DownloadedFile] = []
    failures: list[DownloadFailure] = []

    for resource in planned.resources:
        if (planned.asset_id, resource.key) in skip_keys:
            continue
        target = resource_path(staging_dirs, resource)
        url = resource.url
        if not url:
            failures.append(
                DownloadFailure(planned.asset_id, resource.key, "no download URL")
            )
            continue
        try:
            size = _stream_to_disk(session, url, target, resource.size, disk_budget)
        except Exception as exc:  # noqa: BLE001 - recorded per resource, run continues
            LOGGER.warning(
                "Download failed for %s/%s (%s): %s",
                planned.asset_id,
                resource.key,
                resource.filename,
                exc,
            )
            failures.append(DownloadFailure(
                planned.asset_id, resource.key, str(exc),
                capacity_limited=isinstance(exc, DiskSpaceError),
            ))
            continue
        LOGGER.debug("Downloaded %s (%d bytes)", target.name, size)
        files.append(DownloadedFile(planned.asset_id, resource.key, target, size))

    return files, failures


def download_batch(
    batch: list[PlannedAsset],
    staging_dirs: dict[str, Path],
    *,
    workers: int = 4,
    skip_keys: frozenset[tuple[str, str]] = frozenset(),
    disk_headroom_bytes: int = 0,
    reservation_factor: int = 1,
) -> DownloadOutcome:
    """Download a batch of assets concurrently, one worker per asset.

    Resources of a single asset are fetched sequentially so a Live Photo's still
    and video always land together.
    """
    files: list[DownloadedFile] = []
    failures: list[DownloadFailure] = []
    try:
        budget = DiskBudget(staging_dirs["media"], disk_headroom_bytes, reservation_factor)
    except DiskSpaceError as exc:
        return DownloadOutcome(files=[], failures=[
            DownloadFailure(p.asset_id, r.key, str(exc), capacity_limited=True)
            for p in batch for r in p.resources if (p.asset_id, r.key) not in skip_keys
        ])

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dl") as pool:
        futures = {
            pool.submit(
                download_asset, planned, staging_dirs, skip_keys=skip_keys, disk_budget=budget,
            ): planned
            for planned in batch
        }
        for future in as_completed(futures):
            planned = futures[future]
            try:
                ok, bad = future.result()
            except Exception as exc:  # noqa: BLE001 - one asset must not kill the batch
                LOGGER.error("Unexpected download error for %s: %s", planned.asset_id, exc)
                failures.extend(
                    DownloadFailure(planned.asset_id, res.key, str(exc))
                    for res in planned.resources
                )
                continue
            files.extend(ok)
            failures.extend(bad)

    return DownloadOutcome(files=files, failures=failures)
