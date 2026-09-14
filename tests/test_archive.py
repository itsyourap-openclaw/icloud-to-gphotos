# ruff: noqa: F811
"""Durable sidecars must outlive staging and fail closed before source deletion."""

import base64
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from icloud_to_gphotos.assets import plan_asset
from icloud_to_gphotos.config import Settings

from .conftest import FakePhotoAsset
from .test_pipeline import make_pipeline  # noqa: F401


def test_archive_roundtrip_versions_and_sensitive_url_exclusion(settings, tmp_path):
    from icloud_to_gphotos.archive import MetadataArchive
    asset = FakePhotoAsset('../../asset', favorite=True, adjustment_type='portrait')
    asset._asset_record['fields']['captionEnc'] = {'value': base64.b64encode(b'Caption')}
    album = SimpleNamespace(id='album-id', name='Trip', fullname='Travel/Trip', photos=[asset])
    library = SimpleNamespace(zone_id={'zoneName': 'PrimarySync'}, albums=[album])
    store = MetadataArchive(tmp_path / 'archive', 'test@example.invalid', library)
    planned = plan_asset(asset, settings, 'safe-name')
    first = store.write(planned)
    payload = json.loads(first.read_text())
    assert payload['metadata']['title'] == 'Caption'
    assert payload['favorite'] is True
    assert payload['albums'][0]['id'] == 'album-id'
    assert 'cloudkit.invalid' not in first.read_text()
    assert 'downloadURL' not in first.read_text()
    assert first.with_name('metadata.xmp').exists()
    assert store.write(planned) == first
    asset._asset_record['fields']['captionEnc']['value'] = base64.b64encode(b'Changed')
    second = store.write(planned)
    assert second != first and first.exists()
    assert tmp_path.resolve() in second.resolve().parents


def test_archives_are_scoped_by_account_and_library(settings, tmp_path):
    from icloud_to_gphotos.archive import MetadataArchive
    planned = plan_asset(FakePhotoAsset('same-id'), settings, 'same')
    paths = set()
    for account, zone in [('a@example.invalid', 'root'), ('b@example.invalid', 'root'),
                          ('a@example.invalid', 'shared')]:
        library = SimpleNamespace(zone_id={'zoneName': zone}, albums=[])
        paths.add(MetadataArchive(tmp_path, account, library).write(planned))
    assert len(paths) == 3


def test_archive_cannot_be_inside_staging(tmp_path):
    with pytest.raises(ValidationError, match='staging'):
        Settings(icloud_username='test@example.invalid', state_dir=tmp_path,
                 metadata_archive_dir=tmp_path / 'staging' / 'archive', _env_file=None)


def test_archive_failure_retains_source_and_retries(make_pipeline, monkeypatch, tmp_path):
    from icloud_to_gphotos.archive import MetadataArchive
    asset = FakePhotoAsset('archive-failure')
    pipe, session, _, ledger = make_pipeline([asset])
    session.library = SimpleNamespace(zone_id={'zoneName': 'root'}, albums=[])
    pipe.settings.metadata_archive_dir = tmp_path / 'archive'
    real = MetadataArchive.write
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(MetadataArchive, 'write', fail)
    assert pipe.run('failed-archive').totals.purged_assets == 0
    assert ledger.asset_ready_to_purge(asset.id)
    monkeypatch.setattr(MetadataArchive, 'write', real)
    assert pipe.run('retry-archive').totals.purged_assets == 1
    assert len(list((tmp_path / 'archive').rglob('manifest.json'))) == 1


def test_dry_run_does_not_write_archive(make_pipeline, tmp_path):
    pipe, _, _, _ = make_pipeline([FakePhotoAsset('preview')], dry_run=True)
    pipe.settings.metadata_archive_dir = tmp_path / 'archive'
    pipe.run('dry-archive')
    assert not (tmp_path / 'archive').exists()


def test_partial_album_listing_cannot_authorize_deletion(make_pipeline, tmp_path):
    def albums():
        yield SimpleNamespace(id='one', fullname='One', photos=[])
        raise RuntimeError('album listing interrupted')
    pipe, session, _, _ = make_pipeline([FakePhotoAsset('partial-albums')])
    session.library = SimpleNamespace(zone_id={'zoneName': 'root'}, albums=albums())
    pipe.settings.metadata_archive_dir = tmp_path / 'archive'
    assert pipe.run('partial-albums').totals.purged_assets == 0
    assert not list((tmp_path / 'archive').rglob('manifest.json'))
