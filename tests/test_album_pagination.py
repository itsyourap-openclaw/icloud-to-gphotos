# ruff: noqa: F811
"""Exercise real album pagination while deletion changes remote ranks."""

from types import SimpleNamespace

import pytest

from icloud_to_gphotos.selection import select_assets

from .conftest import FakePhotoAsset
from .test_pipeline import make_pipeline  # noqa: F401


def test_selected_ascending_album_skips_assets_after_batch_deletion(make_pipeline):
    import base64

    from pyicloud.services.photos_cloudkit.service import PhotoLibrary

    from icloud_to_gphotos.icloud_client import ICloudSession
    assets = [FakePhotoAsset(f'page-{n}', filename=f'{n}.HEIC') for n in range(8)]
    pipe, session, _, _ = make_pipeline(assets)
    pipe.settings.batch_max_items = 4
    pipe.settings.include_albums = ['selected']
    pipe.settings.incremental_scan = True  # Correctly bypassed for selected-album runs.
    library = SimpleNamespace(service=SimpleNamespace(), _client=None,
                              zone_id={'zoneName': 'PrimarySync'}, albums=[])
    record = {'recordName': 'selected', 'fields': {
        'albumNameEnc': {'value': base64.b64encode(b'Review Album').decode()},
        'albumType': {'value': 0}, 'sortAscending': {'value': 1}}}
    album = PhotoLibrary._convert_record_to_album(library, record)
    assert album._direction.value == 'ASCENDING'
    album._page_size = 4
    offsets = []
    def page(offset, direction, page_size):
        offsets.append(offset)
        remaining = [a for a in assets if not a.delete_calls]
        yield from remaining[offset:offset + page_size]
    # Real converted PhotoAlbum and real session selector; only remote paging is fake.
    album._get_photos_at = page
    library.albums = [album]
    facade = ICloudSession(SimpleNamespace(photos=SimpleNamespace(libraries={'root': library})),
                          settings=pipe.settings)
    session.iter_all_assets = facade.iter_all_assets
    result = pipe.run('selected-album')
    assert result.totals.scanned == 8 and result.library_remaining == 0
    assert result.status == 'ok'
    assert result.totals.purged_assets == 8



def test_selected_album_page_failure_precedes_any_yield():
    def broken():
        yield FakePhotoAsset('first-page')
        raise RuntimeError('later page failed')

    album = SimpleNamespace(id='selected', fullname='Selected', name='Selected', photos=broken())
    library = SimpleNamespace(albums=[album])
    selected = select_assets(library, ['selected'], [])
    with pytest.raises(RuntimeError, match='later page failed'):
        next(selected)
