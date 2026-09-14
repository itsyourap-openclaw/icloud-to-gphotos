"""Incremental CloudKit discovery with durable, independently acknowledged work."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pyicloud.services.photos_cloudkit.mappers import record_field_value

from .config import Settings
from .icloud_client import SourceAssetInvisible
from .ledger import Ledger

LOGGER = logging.getLogger(__name__)


def scan_state_path(settings: Settings, library: Any) -> Path:
    scope = hashlib.sha256(
        json.dumps(
            [
                settings.icloud_username.strip().casefold(),
                library.zone_id,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return settings.state_dir / "scans" / f"{scope}.db"


def preservation_signature(settings: Settings, ledger: Ledger) -> str:
    """Hash semantic preservation requirements, not credentials or tuning options."""
    fields = (
        "edited_policy", "include_live_photo_video", "include_alternative_original",
        "backfill_metadata", "preserve_albums", "pair_live_photos",
        "update_existing_photos_to_live", "ignore_apple_metadata",
        "delete_from_icloud", "delete_grace_days",
    )
    policy = {name: getattr(settings, name) for name in fields}
    for name in ("metadata_archive_dir", "gotohp_config"):
        value = getattr(settings, name)
        policy[name] = str(value.resolve()) if value is not None else None
    policy.update(version=1, ledger=ledger.discovery_identity())
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


class ScanStore:
    """Persist IDs, not expiring resource URLs or serialized cloud credentials."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS checkpoint (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), cursor TEXT, last_full TEXT
            );
            INSERT OR IGNORE INTO checkpoint VALUES (1, NULL, NULL);
            CREATE TABLE IF NOT EXISTS pending (asset_id TEXT PRIMARY KEY, full_generation TEXT);
            CREATE TABLE IF NOT EXISTS discovery_policy (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), fingerprint TEXT NOT NULL
            );
        """)

    @property
    def cursor(self) -> str | None:
        return self.db.execute("SELECT cursor FROM checkpoint").fetchone()["cursor"]

    @property
    def last_full(self) -> datetime | None:
        raw = self.db.execute("SELECT last_full FROM checkpoint").fetchone()["last_full"]
        return datetime.fromisoformat(raw) if raw else None

    @property
    def policy(self) -> str | None:
        row = self.db.execute(
            "SELECT fingerprint FROM discovery_policy WHERE singleton=1"
        ).fetchone()
        return str(row["fingerprint"]) if row else None

    def enqueue(self, asset_id: str, generation: str | None = None) -> None:
        self.db.execute(
            """INSERT INTO pending VALUES (?, ?) ON CONFLICT(asset_id) DO UPDATE
            SET full_generation=COALESCE(excluded.full_generation, pending.full_generation)""",
            (asset_id, generation),
        )

    def acknowledge(self, asset_id: str) -> None:
        self.db.execute("DELETE FROM pending WHERE asset_id=?", (asset_id,))

    def checkpoint(
        self, cursor: str, generation: str | None = None, *, policy: str | None = None
    ) -> None:
        if not cursor:
            raise RuntimeError("CloudKit did not supply a usable sync cursor")
        self.db.execute("BEGIN")
        try:
            if generation is None:
                self.db.execute("UPDATE checkpoint SET cursor=?", (cursor,))
            else:
                self.db.execute(
                    "UPDATE checkpoint SET cursor=?, last_full=?",
                    (cursor, datetime.now(UTC).isoformat()),
                )
                if policy is not None:
                    self.db.execute("INSERT OR REPLACE INTO discovery_policy VALUES (1, ?)",
                                    (policy,))
                # Full enumeration proves these IDs are no longer in the selected view.
                # Do not change upload/purge evidence in the migration ledger.
                self.db.execute(
                    "DELETE FROM pending WHERE full_generation IS NULL OR full_generation != ?",
                    (generation,),
                )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def pending_ids(self) -> Iterator[str]:
        # Keyset pagination permits acknowledgement during batch processing.
        after = ""
        while rows := self.db.execute(
            "SELECT asset_id FROM pending WHERE asset_id > ? ORDER BY asset_id LIMIT 256",
            (after,),
        ).fetchall():
            for row in rows:
                after = row["asset_id"]
                yield after

    def pending_count(self) -> int:
        return int(self.db.execute("SELECT count(*) FROM pending").fetchone()[0])

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> ScanStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class ChangeScanner:
    """Checkpoint complete discovery separately from resource processing/retries."""

    def __init__(self, settings: Settings, session: Any, ledger: Ledger) -> None:
        self.settings, self.session, self.ledger = settings, session, ledger
        self.library = session.library
        self.store = ScanStore(scan_state_path(settings, self.library))
        self.policy = preservation_signature(settings, ledger)
        self.mode = "full"
        self.events = 0
        self.lookup_failures = 0

    def _full(self) -> Iterator[Any]:
        self.mode = "full"
        # Capture BEFORE listing: changes arriving during traversal must be replayed.
        cursor = self.library.sync_cursor()
        generation = uuid.uuid4().hex
        for asset in self.session.iter_all_assets():
            self.store.enqueue(str(asset.id), generation)
            yield asset
        # Not reached on a batch cap, failed page, generator close or interruption.
        self.store.checkpoint(cursor, generation, policy=self.policy)

    def assets(self) -> Iterator[Any]:
        last_full = self.store.last_full
        if (
            self.store.policy != self.policy
            or not self.store.cursor
            or last_full is None
            or (
                datetime.now(UTC) - last_full
                >= timedelta(hours=self.settings.full_scan_interval_hours)
            )
        ):
            yield from self._full()
            return
        self.mode = "delta"
        full_needed = False
        try:
            for event in self.library.iter_changes(since=self.store.cursor):
                self.events += 1
                if event.deleted:
                    self.store.acknowledge(event.record_name)
                elif event.record_type == "CPLAsset":
                    self.store.enqueue(event.record_name)
                elif event.record_type == "CPLMaster":
                    ids = self.ledger.asset_ids_for_master(event.record_name)
                    if not ids:
                        full_needed = True
                    for asset_id in ids:
                        self.store.enqueue(asset_id)
                else:
                    # Album membership / unknown records can affect unchanged assets.
                    full_needed = True
        except Exception as exc:
            # Do not turn authentication, transport, or arbitrary parsing failures
            # into a costly full scan. Only a recognizable invalid cursor does so.
            payload = getattr(exc, "payload", {})
            detail = (str(exc) + json.dumps(payload, default=str)).upper().replace(" ", "_")
            if not any(
                code in detail
                for code in (
                    "CHANGE_TOKEN_EXPIRED",
                    "SYNC_TOKEN_EXPIRED",
                    "INVALID_SYNC_TOKEN",
                )
            ):
                raise
            LOGGER.info("CloudKit cursor expired; performing full reconciliation.")
            self.library.current_sync_token = None
            full_needed = True
        if full_needed:
            yield from self._full()
            return
        # All events are durably queued before the page cursor is advanced.
        self.store.checkpoint(self.library.current_sync_token)
        for asset_id in self.store.pending_ids():
            try:
                asset = self.session.get_asset_by_id(asset_id)
                if asset is None:
                    raise LookupError("asset is not yet visible")
                if any(
                    record_field_value(asset._asset_record, flag)
                    for flag in ("isDeleted", "isHidden")
                ):
                    self.store.acknowledge(asset_id)
                    continue
            except SourceAssetInvisible:
                self.store.acknowledge(asset_id)
                continue
            except Exception as exc:
                self.lookup_failures += 1
                LOGGER.warning("Queued asset lookup deferred (%s)", type(exc).__name__)
                continue
            yield asset

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "events": self.events,
            "pending": self.store.pending_count(),
            "lookup_failures": self.lookup_failures,
            "checkpointed": bool(self.store.cursor),
        }

    def close(self) -> None:
        self.store.close()
