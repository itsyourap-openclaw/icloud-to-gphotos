"""Configuration loaded from environment variables and an optional ``.env`` file."""

from __future__ import annotations

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
        return self

    # --- Derived paths ------------------------------------------------------
    @property
    def cookie_dir(self) -> Path:
        """Directory holding the persisted iCloud session cookies."""
        return self.state_dir / "cookies"

    @property
    def ledger_path(self) -> Path:
        """SQLite database tracking per-resource migration state."""
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
