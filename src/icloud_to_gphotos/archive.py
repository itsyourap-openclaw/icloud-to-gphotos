"""Versioned, local JSON/XMP archives. Never copy CloudKit credentials or URLs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pyicloud.services.photos_cloudkit.mappers import record_field_value
from pyicloud.services.photos_cloudkit.materialize import build_xmp_metadata, write_xmp_sidecar
from pyicloud.services.photos_cloudkit.service import PhotoAlbumFolder, SmartPhotoAlbum

from .assets import PlannedAsset


def _json(value: Any) -> bytes:
    def encode(item: Any) -> str:
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError(f'Unsupported metadata type: {type(item).__name__}')
    return (json.dumps(value, default=encode, ensure_ascii=False, sort_keys=True, indent=2)
            + '\n').encode('utf-8')


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _flush_directory(path: Path) -> None:
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class MetadataArchive:
    """Archive one complete metadata revision atomically, retaining older revisions."""

    def __init__(self, root: Path, account: str, library: Any) -> None:
        self.root = root
        self.library = library
        self.scope = _hash(_json([account.strip().casefold(), library.zone_id]))
        self._memberships: dict[str, list[dict[str, str]]] | None = None
        self._listing_error: Exception | None = None

    def _albums(self) -> dict[str, list[dict[str, str]]]:
        if self._listing_error is not None:
            raise RuntimeError('Album metadata listing was incomplete') from self._listing_error
        if self._memberships is None:
            found: dict[str, list[dict[str, str]]] = {}
            try:
                for album in self.library.albums:
                    if isinstance(album, (PhotoAlbumFolder, SmartPhotoAlbum)):
                        continue
                    entry = {'id': str(album.id), 'name': str(album.fullname)}
                    for asset in album.photos:
                        found.setdefault(str(asset.id), []).append(entry)
            except Exception as exc:
                self._listing_error = exc
                raise
            self._memberships = found
        return self._memberships

    def write(self, planned: PlannedAsset) -> Path:
        """Return the committed manifest, or raise so source deletion stays blocked."""
        memberships = self._albums().get(planned.asset_id, [])
        record = planned.asset._asset_record
        metadata = build_xmp_metadata(record)
        if metadata is None:
            raise ValueError('iCloud asset metadata is missing')
        payload = {
            'format_version': 1,
            'source_scope': self.scope,
            'asset_id': planned.asset_id,
            'master_id': planned.master_id,
            'filename': planned.filename,
            'staged_stem': planned.stem,
            'asset_date': planned.asset_date,
            'added_date': planned.added_date,
            'favorite': planned.is_favorite,
            'hidden': bool(record_field_value(record, 'isHidden')),
            'live_photo': planned.is_live_photo,
            'has_adjustments': planned.has_adjustments,
            'adjustment_type': record_field_value(record, 'adjustmentType'),
            'metadata': asdict(metadata),
            'albums': sorted(memberships, key=lambda item: item['id']),
            'resources': [
                {'key': res.key, 'filename': res.filename, 'size': res.size,
                 'checksum': res.resource.checksum, 'type': res.resource.type}
                for res in planned.resources
            ],
            'available_source_resources': [
                {'key': key, 'filename': res.filename, 'size': res.size,
                 'checksum': res.checksum, 'type': res.type}
                for key, res in sorted(planned.asset.resources.items())
            ],
            'preservation_errors': planned.preservation_errors,
        }
        directory = self.root / self.scope / _hash(planned.asset_id.encode())
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix='.pending-', dir=directory) as scratch:
            pending = Path(scratch)
            write_xmp_sidecar(path=pending / 'metadata', asset_record=record, dry_run=False)
            xmp = (pending / 'metadata.xmp').read_bytes()
            payload['xmp_sha256'] = _hash(xmp)
            manifest = _json(payload)
            revision = directory / _hash(manifest)
            if revision.exists():
                if (revision / 'manifest.json').read_bytes() != manifest or (
                    revision / 'metadata.xmp'
                ).read_bytes() != xmp:
                    raise OSError('Existing metadata archive is incomplete or corrupted')
            else:
                (pending / 'manifest.json').write_bytes(manifest)
                for path in pending.iterdir():
                    path.chmod(0o600)
                    with path.open('rb') as stream:
                        os.fsync(stream.fileno())
                _flush_directory(pending)
                # Both files become visible together; interrupted attempts remain .pending-*.
                pending.rename(revision)
            with tempfile.NamedTemporaryFile(dir=directory, prefix='.latest-', delete=False) as f:
                pointer = Path(f.name)
                try:
                    f.write(_json({'revision': revision.name}))
                    f.flush()
                    os.fsync(f.fileno())
                except BaseException:
                    f.close()
                    pointer.unlink(missing_ok=True)
                    raise
            try:
                pointer.replace(directory / 'latest.json')
            finally:
                pointer.unlink(missing_ok=True)
            for parent in (directory, directory.parent, self.root, self.root.parent):
                _flush_directory(parent)
        return revision / 'manifest.json'
