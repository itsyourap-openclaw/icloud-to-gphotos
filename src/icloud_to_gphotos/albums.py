"""Preserve iCloud album memberships via gotohp's existing album support."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pyicloud.services.photos_cloudkit.service import PhotoAlbumFolder, SmartPhotoAlbum

from .assets import PlannedAsset
from .config import Settings
from .downloader import resource_path
from .ledger import Ledger
from .uploader import UploadError, upload_directory


def album_state_path(settings: Settings, library: Any) -> Path:
    scope = hashlib.sha256(json.dumps([
        settings.icloud_username.strip().casefold(), library.zone_id,
    ], sort_keys=True).encode()).hexdigest()
    return settings.state_dir / 'albums' / f'{scope}.db'


def verify_album_support(binary: Path) -> None:
    try:
        result = subprocess.run([str(binary), 'upload', '--help'], capture_output=True,
                                text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UploadError('Cannot probe gotohp album support') from exc
    if result.returncode or '--album' not in result.stdout + result.stderr:
        raise UploadError('gotohp lacks --album support; rebuild with scripts/fetch_gotohp.py')


class AlbumStore:
    """Commit create intent before remote calls; never guess after a lost response."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS mappings (album_id TEXT PRIMARY KEY, media_key TEXT);
            CREATE TABLE IF NOT EXISTS receipts (
                album_id TEXT, asset_id TEXT, signature TEXT NOT NULL,
                PRIMARY KEY (album_id, asset_id)
            );
        ''')

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> AlbumStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def key(self, album_id: str) -> str | None:
        row = self.db.execute('SELECT media_key FROM mappings WHERE album_id=?',
                              (album_id,)).fetchone()
        return row['media_key'] if row else None

    def target(self, album_id: str, name: str) -> str:
        row = self.db.execute('SELECT media_key FROM mappings WHERE album_id=?',
                              (album_id,)).fetchone()
        if row:
            if row['media_key']:
                return str(row['media_key'])
            raise UploadError(f'Album {album_id}: creation outcome unknown; use i2g bind-album.')
        self.db.execute('INSERT INTO mappings VALUES (?, NULL)', (album_id,))
        return 'iCloud / ' + name

    def bind(self, album_id: str, key: str) -> None:
        # gotohp treats this prefix as an album key and slices its first ten characters.
        if not re.fullmatch(r'AF1Qip[A-Za-z0-9_-]{4,}', key):
            raise ValueError('A complete Google Photos album key beginning AF1Qip is required.')
        self.db.execute('INSERT OR REPLACE INTO mappings VALUES (?, ?)', (album_id, key))

    def confirmed(self, album_id: str, asset_id: str, signature: str) -> bool:
        return self.db.execute(
            'SELECT 1 FROM receipts WHERE album_id=? AND asset_id=? AND signature=?',
            (album_id, asset_id, signature),
        ).fetchone() is not None

    def confirm(self, album_id: str, asset_id: str, signature: str) -> None:
        self.db.execute('INSERT OR REPLACE INTO receipts VALUES (?, ?, ?)',
                        (album_id, asset_id, signature))

    def mappings(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute('SELECT * FROM mappings ORDER BY album_id')]


class AlbumSync:
    """One run's complete membership snapshot and verified destination mappings."""

    def __init__(self, settings: Settings, library: Any, ledger: Ledger, binary: Path) -> None:
        verify_album_support(binary)
        self.settings, self.ledger, self.binary = settings, ledger, binary
        self.memberships: dict[str, list[str]] = {}
        self.names: dict[str, str] = {}
        # Finish enumeration before any asset may be deleted.
        for album in library.albums:
            if isinstance(album, (PhotoAlbumFolder, SmartPhotoAlbum)):
                continue
            self.names[str(album.id)] = str(album.fullname)
            for asset in album.photos:
                self.memberships.setdefault(str(asset.id), []).append(str(album.id))
        self.store = AlbumStore(album_state_path(settings, library))

    def _signature(self, planned: PlannedAsset, album_id: str) -> str:
        rows = {r.resource_key: r for r in self.ledger.get_resources(planned.asset_id)}
        media = []
        for res in planned.resources:
            row = rows.get(res.key)
            media.append((res.key, res.resource.checksum, res.size,
                          row.media_key if row and row.is_uploaded else None))
        return hashlib.sha256(json.dumps([self.store.key(album_id), media]).encode()).hexdigest()

    def pending(self, planned: PlannedAsset) -> bool:
        return any(
            not self.store.confirmed(album_id, planned.asset_id, self._signature(planned, album_id))
            for album_id in self.memberships.get(planned.asset_id, [])
        )

    def sync(self, planned: PlannedAsset, staging: dict[str, Path]) -> None:
        for album_id in self.memberships.get(planned.asset_id, []):
            if self.store.confirmed(album_id, planned.asset_id, self._signature(planned, album_id)):
                continue
            # One representative path per destination item, including a linked Live Photo.
            expected: dict[str, Path] = {}
            for res in planned.resources:
                row = self.ledger.get_resource(planned.asset_id, res.key)
                if row is None or not row.is_uploaded or not row.media_key:
                    raise UploadError('Album sync needs confirmed destination media keys.')
                expected.setdefault(row.media_key, resource_path(staging, res))
            if not expected or any(not path.is_file() for path in expected.values()):
                raise UploadError('Album sync needs staged media for gotohp hash lookup.')
            with tempfile.TemporaryDirectory(prefix='album-', dir=staging['media'].parent) as tmp:
                directory = Path(tmp)
                by_path = {}
                for key, path in expected.items():
                    linked = directory / path.name
                    os.link(path, linked)
                    by_path[str(linked.resolve())] = key
                target = self.store.target(album_id, self.names[album_id])
                report = upload_directory(
                    directory, binary=self.binary, threads=self.settings.upload_threads,
                    pair_live_photos=False, config_path=self.settings.gotohp_config, album=target,
                )
                summary = report.album or {}
                keys = summary.get('albumKeys')
                if isinstance(keys, list) and len(keys) == 1 and isinstance(keys[0], str):
                    current = self.store.key(album_id)
                    if current is not None and current != keys[0]:
                        raise UploadError('gotohp acknowledged a different destination album.')
                    self.store.bind(album_id, keys[0])
                verdicts: dict[str, list[Any]] = {}
                for verdict in report.verdicts:
                    if verdict.path is not None:
                        verdicts.setdefault(verdict.path, []).append(verdict)
                if not (
                    self.store.key(album_id) and keys == [self.store.key(album_id)]
                    and type(summary.get('itemsAdded')) is int
                    and summary.get('itemsAdded') == len(expected) and not summary.get('error')
                    and report.exit_code == 0
                    and set(verdicts) == set(by_path)
                    and all(len(verdicts[path]) == 1 and verdicts[path][0].uploaded
                            and verdicts[path][0].media_key == key for path, key in by_path.items())
                ):
                    raise UploadError('Album membership was not fully acknowledged.')
                self.store.confirm(album_id, planned.asset_id, self._signature(planned, album_id))

    def close(self) -> None:
        self.store.close()
