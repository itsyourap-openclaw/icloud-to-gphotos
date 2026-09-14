"""Configuration loaded from environment variables and an optional ``.env`` file."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GiB = 1024**3

EditedPolicy = Literal["both", "edited", "original"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


def default_state_dir(os_name: str | None = None) -> Path:
    """Return the platform-appropriate directory for persistent state.

    Args:
        os_name: Override for ``os.name``, so both branches are testable from
            either platform.
    """
    if (os_name or os.name) == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "icloud-to-gphotos"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "icloud-to-gphotos"


class Settings(BaseSettings):
    """Runtime settings for the migration pipeline.

    Every field is settable via an ``I2G_``-prefixed environment variable or an
    entry in ``.env``; see ``.env.example`` for the documented set.
    """

    model_config = SettingsConfigDict(
        env_prefix="I2G_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- iCloud credentials -------------------------------------------------
    icloud_username: str = Field(description="Apple ID email address.")
    icloud_password: str | None = Field(
        default=None,
        description=(
            "Apple ID password. Only needed for the initial `i2g login`; once a "
            "trusted session exists it is not read again."
        ),
    )

    # Explicit source selection is opt-in; unconfigured installs retain their ledger.
    icloud_library: str | None = None
    include_albums: list[str] = Field(default_factory=list)
    exclude_albums: list[str] = Field(default_factory=list)

    @field_validator("icloud_library")
    @classmethod
    def _library_key(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value.strip() == "shared"):
            raise ValueError("Use root or an exact library ID; legacy shared streams unsupported.")
        return value.strip() if value is not None else None

    @field_validator("include_albums", "exclude_albums")
    @classmethod
    def _album_selectors(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("Album selectors cannot be empty.")
        return list(dict.fromkeys(value.strip() for value in values))

    @model_validator(mode="after")
    def _source_delete_policy(self) -> Settings:
        if self.icloud_library not in (None, "root") and self.delete_from_icloud:
            raise ValueError(
                "Non-root libraries are backup-only: set I2G_DELETE_FROM_ICLOUD=false."
            )
        return self

    # --- Paths --------------------------------------------------------------
    state_dir: Path = Field(default_factory=default_state_dir)
    staging_dir: Path | None = Field(
        default=None,
        description="Scratch directory for downloads. Defaults to <state_dir>/staging.",
    )
    gotohp_binary: Path | None = Field(
        default=None,
        description="Path to gotohp-cli. Auto-discovered on PATH and ./bin when unset.",
    )
    gotohp_config: Path | None = Field(
        default=None,
        description="Path to gotohp's config file holding Google Photos credentials.",
    )
    exiftool_binary: Path | None = Field(
        default=None,
        description="Path to exiftool. Auto-discovered on PATH when unset.",
    )

    # --- Batching -----------------------------------------------------------
    incremental_scan: bool = Field(
        default=False, description="Use a CloudKit cursor and durable queue between full scans."
    )
    full_scan_interval_hours: int = Field(default=168, gt=0)

    batch_max_bytes: int = Field(
        default=20 * GiB,
        gt=0,
        description="Soft cap on bytes downloaded per batch before uploading.",
    )
    batch_max_items: int = Field(
        default=500,
        gt=0,
        description="Cap on assets downloaded per batch before uploading.",
    )
    max_batches_per_run: int | None = Field(
        default=None,
        description="Stop after this many batches. Unset means drain the library.",
    )
    disk_headroom_bytes: int = Field(
        default=5 * GiB,
        ge=0,
        description="Preserve this much free disk during admission and streaming downloads.",
    )

    # --- Behaviour ----------------------------------------------------------
    edited_policy: EditedPolicy = Field(
        default="both",
        description=(
            "For assets with iCloud adjustments: upload the edited render, the "
            "untouched original, or both."
        ),
    )
    include_live_photo_video: bool = Field(default=True)
    preserve_albums: bool = Field(
        default=False, description="Preserve user albums before allowing iCloud deletion."
    )
    include_alternative_original: bool = Field(
        default=False,
        description="Also upload resOriginalAlt (the JPEG beside a ProRAW DNG).",
    )
    delete_from_icloud: bool = Field(
        default=True,
        description="Master switch for iCloud deletion. Set false for download+upload only.",
    )
    delete_grace_days: int = Field(
        default=7,
        ge=0,
        description=(
            "Wait this many days after both capture and import into iCloud. "
            "When the import date is unavailable, use first observation in the ledger."
        ),
    )
    upload_threads: int = Field(default=3, ge=1, le=16)
    download_workers: int = Field(default=4, ge=1, le=16)
    pair_live_photos: bool = Field(default=True)
    update_existing_photos_to_live: bool = Field(
        default=False,
        description="Attach Live Photo motion to an existing Google Photos still (opt-in).",
    )
    @model_validator(mode="after")
    def _validate_live_photo_repair(self) -> Settings:
        if self.update_existing_photos_to_live and (
            not self.pair_live_photos or not self.include_live_photo_video
            or self.edited_policy == "edited"
        ):
            raise ValueError(
                "Live Photo repair requires pairing, motion, and original preservation."
            )
        return self

    ignore_apple_metadata: bool = Field(
        default=False,
        description=(
            "Pair Live Photos by filename stem instead of Apple content identifier. "
            "Only needed if your exports have stripped identifiers."
        ),
    )
    backfill_metadata: bool = Field(
        default=True,
        description="Use exiftool to write capture date/GPS into files that lack them.",
    )
    metadata_archive_dir: Path | None = Field(
        default=None,
        description="Permanent JSON/XMP archive directory; unset disables archiving.",
    )

    # --- Notifications ------------------------------------------------------
    ntfy_topic: str | None = Field(
        default=None, description="ntfy.sh topic name, e.g. 'my-icloud-sync'."
    )
    ntfy_server: str = Field(default="https://ntfy.sh")
    ntfy_token: str | None = Field(default=None, description="Bearer token for private ntfy.")
    notify_on_success: bool = Field(default=True)

    # --- Logging ------------------------------------------------------------
    log_level: LogLevel = Field(default="INFO")
    log_retention: int = Field(default=30, ge=1, description="Run logs/reports to keep.")

    @field_validator(
        "state_dir",
        "staging_dir",
        "gotohp_binary",
        "gotohp_config",
        "exiftool_binary",
        "metadata_archive_dir",
        mode="before",
    )
    @classmethod
    def _expand(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                return None
            return Path(value).expanduser()
        if isinstance(value, Path):
            return value.expanduser()
        return value

    @model_validator(mode="after")
    def _derive_paths(self) -> Settings:
        if self.staging_dir is None:
            object.__setattr__(self, "staging_dir", self.state_dir / "staging")
        if self.metadata_archive_dir is not None:
            archive = self.metadata_archive_dir.resolve()
            for temporary in (self.staging_dir, self.log_dir, self.report_dir):
                if temporary is not None and archive.is_relative_to(temporary.resolve()):
                    raise ValueError("Metadata archive must be outside staging and rotated logs.")
        return self

    # --- Derived paths ------------------------------------------------------
    @property
    def cookie_dir(self) -> Path:
        """Directory holding the persisted iCloud session cookies."""
        return self.state_dir / "cookies"

    @property
    def ledger_path(self) -> Path:
        """SQLite database tracking per-resource migration state."""
        if self.icloud_library is not None or self.include_albums or self.exclude_albums:
            scope = hashlib.sha256(json.dumps([
                self.icloud_username.strip().casefold(), self.icloud_library or "root"
            ]).encode()).hexdigest()
            return self.state_dir / "sources" / scope / "ledger.db"
        return self.state_dir / "ledger.db"

    @property
    def log_dir(self) -> Path:
        """Directory holding per-run log files."""
        return self.state_dir / "logs"

    @property
    def report_dir(self) -> Path:
        """Directory holding per-run JSON reports."""
        return self.state_dir / "reports"

    @property
    def media_staging_dir(self) -> Path:
        """Staging subtree for originals and Live Photo video components."""
        assert self.staging_dir is not None
        return self.staging_dir / "media"

    @property
    def edited_staging_dir(self) -> Path:
        """Staging subtree for edited renders, uploaded without Live Photo pairing."""
        assert self.staging_dir is not None
        return self.staging_dir / "edited"

    def ensure_dirs(self) -> None:
        """Create every directory the pipeline writes to."""
        for path in (
            self.state_dir,
            self.cookie_dir,
            self.log_dir,
            self.report_dir,
            self.media_staging_dir,
            self.edited_staging_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(**overrides: object) -> Settings:
    """Build settings from the environment, applying explicit overrides last."""
    return Settings(**overrides)  # type: ignore[arg-type]
