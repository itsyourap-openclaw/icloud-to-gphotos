"""Exact album selection without silently broadening migration scope."""

from collections.abc import Iterator
from typing import Any

from pyicloud.services.photos_cloudkit.mappers import record_field_value
from pyicloud.services.photos_cloudkit.service import PhotoAlbumFolder


def _resolve(albums: list[Any], selectors: list[str]) -> list[Any]:
    resolved = []
    for selector in selectors:
        exact = [album for album in albums if album.id == selector]
        paths = [album for album in albums if album.fullname == selector]
        names = [album for album in albums if album.name == selector]
        matches = exact or paths or names
        if len(matches) != 1:
            raise ValueError(f'Album {selector!r} is missing or ambiguous; use an album ID.')
        album = matches[0]
        if isinstance(album, PhotoAlbumFolder):
            raise ValueError(f'{selector!r} is a folder; select its individual albums.')
        resolved.append(album)
    return resolved


def select_assets(library: Any, includes: list[str], excludes: list[str]) -> Iterator[Any]:
    """Yield the include union minus exclusions, resolving all selectors first."""
    albums = list(library.albums)
    selected = _resolve(albums, includes)
    excluded = _resolve(albums, excludes)
    # Materialize exclusions before yielding anything: a partial listing must not
    # accidentally authorize migration/deletion of an excluded item.
    excluded_ids = {str(asset.id) for album in excluded for asset in album.photos}
    seen: set[str] = set()
    sources = selected if includes else [library.all]
    for source in sources:
        for asset in source.photos:
            key = str(asset.id)
            if key in seen or key in excluded_ids:
                continue
            seen.add(key)
            if any(record_field_value(asset._asset_record, flag)
                   for flag in ('isDeleted', 'isHidden')):
                continue
            yield asset
