"""SQLite ledger tracking the migration state of every asset and resource.

The ledger is the pipeline's memory. It is what makes the whole thing crash-safe
and idempotent: an asset is only deleted from iCloud once every resource
belonging to it is recorded here as confirmed-uploaded by Google Photos.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

SCHEMA_VERSION = 4

ResourceState = Literal["pending", "downloaded", "uploaded", "failed", "purged"]

#: A resource that failed this many upload attempts is reported and left alone
#: rather than retried forever, so one poison file cannot stall the pipeline.
MAX_UPLOAD_ATTEMPTS = 5

_RESET_RESOURCE_STATE = """
UPDATE resources
   SET state = 'pending', media_key = NULL, error = NULL, attempts = 0,
       downloaded_at = NULL, uploaded_at = NULL, updated_at = ?
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id        TEXT PRIMARY KEY,
    master_id       TEXT,
    filename        TEXT NOT NULL,
    stem            TEXT NOT NULL,
    item_type       TEXT NOT NULL,
    asset_date      TEXT NOT NULL,
    added_date      TEXT,
    is_live_photo   INTEGER NOT NULL DEFAULT 0,
    has_adjustments INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    purged_at       TEXT,
    purge_error     TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_stem ON assets (stem);
CREATE INDEX IF NOT EXISTS idx_assets_stem_lower ON assets (lower(stem));
CREATE INDEX IF NOT EXISTS idx_assets_alt_lower ON assets (lower(stem || '_alt'));
CREATE INDEX IF NOT EXISTS idx_assets_edited_lower ON assets (lower(stem || '_edited'));
CREATE INDEX IF NOT EXISTS idx_assets_purged ON assets (purged_at);

CREATE TABLE IF NOT EXISTS resources (
    asset_id      TEXT NOT NULL,
    resource_key  TEXT NOT NULL,
    filename      TEXT NOT NULL,
    staging_root  TEXT NOT NULL,
    size          INTEGER,
    checksum      TEXT,
    state         TEXT NOT NULL DEFAULT 'pending',
    media_key     TEXT,
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    downloaded_at TEXT,
    uploaded_at   TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (asset_id, resource_key),
    FOREIGN KEY (asset_id) REFERENCES assets (asset_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_resources_state ON resources (state);

CREATE TABLE IF NOT EXISTS live_photo_pairs (
    asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),
    signature TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    downloaded  INTEGER NOT NULL DEFAULT 0,
    uploaded    INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    purged      INTEGER NOT NULL DEFAULT 0,
    bytes_moved INTEGER NOT NULL DEFAULT 0,
    detail      TEXT
);
"""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class ResourceRow:
    """One downloadable variant of an asset, plus its migration state."""

    asset_id: str
    resource_key: str
    filename: str
    staging_root: str
    size: int | None
    checksum: str | None
    state: ResourceState
    media_key: str | None
    error: str | None
    attempts: int

    @property
    def is_uploaded(self) -> bool:
        """True once Google Photos has confirmed this resource."""
        return self.state in ("uploaded", "purged")

    @property
    def is_exhausted(self) -> bool:
        """True when this resource has failed too many times to keep retrying."""
        return self.state == "failed" and self.attempts >= MAX_UPLOAD_ATTEMPTS


@dataclass(slots=True)
class AssetRow:
    """An iCloud asset as recorded in the ledger."""

    asset_id: str
    filename: str
    stem: str
    item_type: str
    asset_date: str
    is_live_photo: bool
    has_adjustments: bool
    purged_at: str | None
    first_seen_at: str


class Ledger:
    """Thin, explicit SQLite wrapper. One instance per process."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        with self.transaction():
            version = int(self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()["value"])
            if version < 2:
                # Older planners used CPLMaster previews as "edited". Their
                # confirmations cannot prove a current CPLAsset render was
                # uploaded, even when sizes match or fingerprints are absent.
                # Leave already-deleted history alone; recheck retained assets.
                self._conn.execute(
                    _RESET_RESOURCE_STATE + " WHERE resource_key = 'edited' AND state != 'purged'",
                    (_utcnow(),),
                )
            if version < 3:
                # Legacy Live Photo confirmations did not distinguish a whole
                # pair from a skip proving only one component existed remotely.
                self._conn.execute(
                    _RESET_RESOURCE_STATE + " WHERE state != 'purged' AND asset_id IN "
                    "(SELECT asset_id FROM assets WHERE is_live_photo = 1 AND purged_at IS NULL)",
                    (_utcnow(),),
                )
            if version < 4:
                # Old basename-only matching could credit an unrelated resource.
                # No path provenance was stored, so retained confirmations must
                # be re-established once. Preserve deleted history and retries.
                self._conn.execute(
                    _RESET_RESOURCE_STATE + " WHERE state = 'uploaded' AND asset_id IN "
                    "(SELECT asset_id FROM assets WHERE purged_at IS NULL)",
                    (_utcnow(),),
                )
            self._conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
            )

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def snapshot(self) -> Ledger:
        """Return an isolated in-memory copy for planning without state changes."""
        copied = Ledger(Path(":memory:"))
        self._conn.backup(copied._conn)
        return copied

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside a single SQLite transaction."""
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # --- Assets -------------------------------------------------------------

    def prepare_live_photo_pair(self, asset_id: str, signature: str) -> None:
        """Invalidate legacy/separate confirmations once per pair fingerprint."""
        row = self._conn.execute(
            "SELECT signature FROM live_photo_pairs WHERE asset_id = ?", (asset_id,)
        ).fetchone()
        if row is not None and row["signature"] == signature:
            return
        self._conn.execute(
            _RESET_RESOURCE_STATE + " WHERE asset_id = ? AND state != 'purged' "
            "AND resource_key IN ('original', 'original_video')",
            (_utcnow(), asset_id),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO live_photo_pairs VALUES (?, ?, 0)", (asset_id, signature)
        )

    def confirm_live_photo_pair(self, asset_id: str, signature: str) -> None:
        """Record a single successful linked-pair verdict."""
        self._conn.execute(
            "UPDATE live_photo_pairs SET confirmed = 1 WHERE asset_id = ? AND signature = ?",
            (asset_id, signature),
        )

    def live_photo_pair_confirmed(self, asset_id: str, signature: str) -> bool:
        """Check linkage evidence, independently of individual upload states."""
        return self._conn.execute(
            "SELECT 1 FROM live_photo_pairs WHERE asset_id = ? AND signature = ? AND confirmed = 1",
            (asset_id, signature),
        ).fetchone() is not None

    def get_asset(self, asset_id: str) -> AssetRow | None:
        """Return the recorded asset, or None if it has never been seen."""
        row = self._conn.execute(
            "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
        ).fetchone()
        return None if row is None else _to_asset(row)

    def reserve_stem(self, asset_id: str, preferred: str) -> str:
        """Return a filename stem unique across all known assets.

        Live Photo components and edited renders of one asset share this stem, so
        gotohp can pair them, while two distinct assets that happen to be named
        ``IMG_1234`` never collide in the staging directory.
        """
        existing = self._conn.execute(
            "SELECT stem FROM assets WHERE asset_id = ?", (asset_id,)
        ).fetchone()
        if existing is not None:
            preferred = str(existing["stem"])

        candidate = preferred
        suffix = 0
        while True:
            # Reserve the complete rendition stem family, even for resources
            # not currently available. This prevents future edits/alternatives
            # from overwriting another asset or pairing with its video. Compare
            # case-insensitively for Windows and gotohp's filename pairing.
            family = tuple(
                s.casefold() for s in (candidate, candidate + "_alt", candidate + "_edited")
            )
            clash = self._conn.execute(
                "SELECT asset_id FROM assets WHERE asset_id != ? AND ("
                "lower(stem) IN (?, ?, ?) OR lower(stem || '_alt') IN (?, ?, ?) OR "
                "lower(stem || '_edited') IN (?, ?, ?)) LIMIT 1",
                (asset_id, *family, *family, *family),
            ).fetchone()
            if clash is None:
                return candidate
            suffix += 1
            discriminator = asset_id[:8] if suffix == 1 else f"{asset_id[:8]}_{suffix}"
            candidate = f"{preferred}_{discriminator}"

    def upsert_asset(
        self,
        *,
        asset_id: str,
        master_id: str,
        filename: str,
        stem: str,
        item_type: str,
        asset_date: datetime,
        added_date: datetime | None,
        is_live_photo: bool,
        has_adjustments: bool,
    ) -> None:
        """Record or refresh an asset's immutable-ish descriptive fields."""
        self._conn.execute(
            """
            INSERT INTO assets (
                asset_id, master_id, filename, stem, item_type, asset_date,
                added_date, is_live_photo, has_adjustments, first_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (asset_id) DO UPDATE SET
                filename        = excluded.filename,
                stem            = excluded.stem,
                item_type       = excluded.item_type,
                asset_date      = excluded.asset_date,
                added_date      = excluded.added_date,
                is_live_photo   = excluded.is_live_photo,
                has_adjustments = excluded.has_adjustments
            """,
            (
                asset_id,
                master_id,
                filename,
                stem,
                item_type,
                asset_date.isoformat(),
                added_date.isoformat() if added_date else None,
                int(is_live_photo),
                int(has_adjustments),
                _utcnow(),
            ),
        )

    def mark_asset_purged(self, asset_id: str) -> None:
        """Record that the asset was deleted from iCloud."""
        now = _utcnow()
        self._conn.execute(
            "UPDATE assets SET purged_at = ?, purge_error = NULL WHERE asset_id = ?",
            (now, asset_id),
        )
        self._conn.execute(
            "UPDATE resources SET state = 'purged', updated_at = ? WHERE asset_id = ?",
            (now, asset_id),
        )

    def mark_asset_purge_failed(self, asset_id: str, error: str) -> None:
        """Record that deleting the asset from iCloud failed."""
        self._conn.execute(
            "UPDATE assets SET purge_error = ? WHERE asset_id = ?", (error, asset_id)
        )

    def asset_ids_for_master(self, master_id: str) -> list[str]:
        """Map a master-only change event back to retained asset IDs."""
        return [row["asset_id"] for row in self._conn.execute(
            "SELECT asset_id FROM assets WHERE master_id = ? AND purged_at IS NULL", (master_id,)
        )]

    # --- Resources ----------------------------------------------------------

    def get_resource(self, asset_id: str, resource_key: str) -> ResourceRow | None:
        """Return one resource row, or None if unknown."""
        row = self._conn.execute(
            "SELECT * FROM resources WHERE asset_id = ? AND resource_key = ?",
            (asset_id, resource_key),
        ).fetchone()
        return None if row is None else _to_resource(row)

    def get_resources(self, asset_id: str) -> list[ResourceRow]:
        """Return every recorded resource for an asset."""
        rows = self._conn.execute(
            "SELECT * FROM resources WHERE asset_id = ? ORDER BY resource_key",
            (asset_id,),
        ).fetchall()
        return [_to_resource(row) for row in rows]

    def upsert_resource(
        self,
        *,
        asset_id: str,
        resource_key: str,
        filename: str,
        staging_root: str,
        size: int | None,
        checksum: str | None,
    ) -> ResourceRow:
        """Preserve progress only while the resource still identifies the same bytes."""
        previous = self.get_resource(asset_id, resource_key)
        if previous is not None and (
            (checksum is not None and checksum != previous.checksum)
            or (size is not None and previous.size is not None and size != previous.size)
            or filename != previous.filename
        ):
            # Reset before replacing identity fields: a crash must never attach
            # an old confirmation (or exhausted retries) to a newer render.
            self._conn.execute(
                _RESET_RESOURCE_STATE + " WHERE asset_id = ? AND resource_key = ?",
                (_utcnow(), asset_id, resource_key),
            )
        self._conn.execute(
            """
            INSERT INTO resources (
                asset_id, resource_key, filename, staging_root, size, checksum,
                state, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            ON CONFLICT (asset_id, resource_key) DO UPDATE SET
                filename     = excluded.filename,
                staging_root = excluded.staging_root,
                size         = COALESCE(excluded.size, resources.size),
                checksum     = COALESCE(excluded.checksum, resources.checksum),
                updated_at   = excluded.updated_at
            """,
            (asset_id, resource_key, filename, staging_root, size, checksum, _utcnow()),
        )
        row = self.get_resource(asset_id, resource_key)
        assert row is not None
        return row

    def mark_downloaded(self, asset_id: str, resource_key: str, size: int) -> None:
        """Record that the resource's bytes are on local disk."""
        now = _utcnow()
        self._conn.execute(
            """
            UPDATE resources
               SET state = 'downloaded', size = ?, downloaded_at = ?, error = NULL,
                   updated_at = ?
             WHERE asset_id = ? AND resource_key = ?
            """,
            (size, now, now, asset_id, resource_key),
        )

    def mark_uploaded(self, asset_id: str, resource_key: str, media_key: str | None) -> None:
        """Record that Google Photos confirmed the resource."""
        now = _utcnow()
        self._conn.execute(
            """
            UPDATE resources
               SET state = 'uploaded', media_key = ?, uploaded_at = ?, error = NULL,
                   updated_at = ?
             WHERE asset_id = ? AND resource_key = ?
            """,
            (media_key, now, now, asset_id, resource_key),
        )

    def mark_failed(self, asset_id: str, resource_key: str, error: str) -> None:
        """Record an upload or download failure and bump the attempt counter."""
        self._conn.execute(
            """
            UPDATE resources
               SET state = 'failed', error = ?, attempts = attempts + 1, updated_at = ?
             WHERE asset_id = ? AND resource_key = ?
            """,
            (error[:2000], _utcnow(), asset_id, resource_key),
        )

    def mark_deferred(self, asset_id: str, resource_key: str, error: str) -> None:
        """Retry capacity-limited work later without exhausting media retries."""
        self._conn.execute(
            "UPDATE resources SET state = 'pending', error = ?, updated_at = ? "
            "WHERE asset_id = ? AND resource_key = ?",
            (error[:2000], _utcnow(), asset_id, resource_key),
        )

    def asset_ready_to_purge(self, asset_id: str) -> bool:
        """True when every recorded resource of the asset is confirmed uploaded.

        An asset with no recorded resources is never ready; that would mean we
        have no evidence anything reached Google Photos.
        """
        rows = self.get_resources(asset_id)
        return bool(rows) and all(row.is_uploaded for row in rows)

    def asset_blocked(self, asset_id: str) -> bool:
        """True when the asset can never be purged without operator action."""
        return any(row.is_exhausted for row in self.get_resources(asset_id))

    # --- Reporting ----------------------------------------------------------

    def start_run(self, run_id: str) -> None:
        """Open a run record."""
        self._conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at) VALUES (?, ?)",
            (run_id, _utcnow()),
        )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        downloaded: int,
        uploaded: int,
        failed: int,
        purged: int,
        bytes_moved: int,
        detail: str | None = None,
    ) -> None:
        """Close a run record with its outcome counters."""
        self._conn.execute(
            """
            UPDATE runs
               SET finished_at = ?, status = ?, downloaded = ?, uploaded = ?,
                   failed = ?, purged = ?, bytes_moved = ?, detail = ?
             WHERE run_id = ?
            """,
            (_utcnow(), status, downloaded, uploaded, failed, purged, bytes_moved, detail, run_id),
        )

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        """Return the most recent run records, newest first."""
        return self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def stats(self) -> dict[str, int]:
        """Return aggregate counters for `i2g status`."""
        counts = {
            f"resources_{row['state']}": row["n"]
            for row in self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM resources GROUP BY state"
            )
        }
        assets = self._conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN purged_at IS NOT NULL THEN 1 ELSE 0 END) AS purged
              FROM assets
            """
        ).fetchone()
        counts["assets_total"] = assets["total"] or 0
        counts["assets_purged"] = assets["purged"] or 0
        counts["bytes_uploaded"] = (
            self._conn.execute(
                "SELECT COALESCE(SUM(size), 0) AS n FROM resources "
                "WHERE state IN ('uploaded', 'purged')"
            ).fetchone()["n"]
            or 0
        )
        return counts

    def discovery_identity(self) -> str:
        """Stable DB identity: a recreated ledger must not inherit an old scan cursor."""
        self._conn.execute(
            "INSERT OR IGNORE INTO meta VALUES ('discovery_identity', lower(hex(randomblob(16))))"
        )
        return str(self._conn.execute(
            "SELECT value FROM meta WHERE key = 'discovery_identity'"
        ).fetchone()["value"])

    def blocked_resources(self, limit: int = 50) -> list[ResourceRow]:
        """Return resources that have exhausted their retry budget."""
        rows = self._conn.execute(
            "SELECT * FROM resources WHERE state = 'failed' AND attempts >= ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (MAX_UPLOAD_ATTEMPTS, limit),
        ).fetchall()
        return [_to_resource(row) for row in rows]


def _to_resource(row: sqlite3.Row) -> ResourceRow:
    return ResourceRow(
        asset_id=row["asset_id"],
        resource_key=row["resource_key"],
        filename=row["filename"],
        staging_root=row["staging_root"],
        size=row["size"],
        checksum=row["checksum"],
        state=row["state"],
        media_key=row["media_key"],
        error=row["error"],
        attempts=row["attempts"],
    )


def _to_asset(row: sqlite3.Row) -> AssetRow:
    return AssetRow(
        asset_id=row["asset_id"],
        filename=row["filename"],
        stem=row["stem"],
        item_type=row["item_type"],
        asset_date=row["asset_date"],
        is_live_photo=bool(row["is_live_photo"]),
        has_adjustments=bool(row["has_adjustments"]),
        purged_at=row["purged_at"],
        first_seen_at=row["first_seen_at"],
    )


def resource_states(rows: Sequence[ResourceRow]) -> dict[str, int]:
    """Summarise a set of resources by state, for logging."""
    out: dict[str, int] = {}
    for row in rows:
        out[row.state] = out.get(row.state, 0) + 1
    return out
