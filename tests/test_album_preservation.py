# ruff: noqa: F811
"""Album acknowledgements and stable ID mappings gate source deletion."""

from types import SimpleNamespace

import pytest

from icloud_to_gphotos.uploader import UploadError, parse_summary

from .conftest import FakePhotoAsset
from .test_pipeline import make_pipeline  # noqa: F401


def configure(pipe, session, assets, monkeypatch, *, fail=False, wrong_key=False):
    from icloud_to_gphotos import albums
    pipe.settings.preserve_albums = True
    session.library = SimpleNamespace(zone_id={'zoneName': 'PrimarySync'}, albums=[
        SimpleNamespace(id='source-album', fullname='Travel/Trip', photos=assets),
    ])
    monkeypatch.setattr(albums, 'verify_album_support', lambda _: None)
    calls = []
    def album_upload(directory, **kwargs):
        files = [p for p in directory.rglob('*') if p.is_file()]
        calls.append({'target': kwargs['album'], 'files': [p.name for p in files]})
        if fail:
            raise UploadError('acknowledgement lost')
        rows = {row.filename: row for a in assets for row in pipe.ledger.get_resources(a.id)}
        return parse_summary({'results': [
            {'path': str(p), 'skipped': True, 'skipCode': 'remote-duplicate',
             'mediaKey': 'wrong-key' if wrong_key else rows[p.name].media_key}
            for p in files
        ], 'album': {'name': kwargs['album'], 'itemsAdded': len(files),
                     'albumKeys': ['AF1Qip-destination-album']}})
    monkeypatch.setattr(albums, 'upload_directory', album_upload)
    return calls


def test_album_ids_reused_across_batches(make_pipeline, monkeypatch):
    assets = [FakePhotoAsset('one', filename='one.HEIC'),
              FakePhotoAsset('two', filename='two.HEIC')]
    pipe, session, _, _ = make_pipeline(assets)
    pipe.settings.batch_max_items = 1
    calls = configure(pipe, session, assets, monkeypatch)
    result = pipe.run('albums')
    assert result.totals.purged_assets == 2
    assert [c['target'] for c in calls] == ['iCloud / Travel/Trip', 'AF1Qip-destination-album']


def test_ambiguous_creation_never_automatically_recreates(make_pipeline, monkeypatch):
    asset = FakePhotoAsset('lost-ack')
    pipe, session, _, ledger = make_pipeline([asset])
    calls = configure(pipe, session, [asset], monkeypatch, fail=True)
    assert pipe.run('lost').totals.purged_assets == 0
    assert ledger.asset_ready_to_purge(asset.id)
    assert pipe.run('retry').totals.purged_assets == 0
    assert len(calls) == 1


def test_explicit_mapping_recovers_unknown_creation(make_pipeline, monkeypatch):
    from icloud_to_gphotos.albums import AlbumStore, album_state_path
    asset = FakePhotoAsset('recover')
    pipe, session, _, _ = make_pipeline([asset])
    configure(pipe, session, [asset], monkeypatch, fail=True)
    pipe.run('lost')
    path = album_state_path(pipe.settings, session.library, pipe.ledger.destination_identity())
    with AlbumStore(path) as store:
        store.bind('source-album', 'AF1Qip-destination-album')
    calls = configure(pipe, session, [asset], monkeypatch)
    assert pipe.run('recovered').totals.purged_assets == 1
    assert calls[0]['target'] == 'AF1Qip-destination-album'


def test_wrong_media_key_cannot_authorize_deletion(make_pipeline, monkeypatch):
    asset = FakePhotoAsset('wrong-key')
    pipe, session, _, _ = make_pipeline([asset])
    configure(pipe, session, [asset], monkeypatch, wrong_key=True)
    assert pipe.run('mismatched-key').totals.purged_assets == 0


def test_completed_membership_is_not_repeated(make_pipeline, monkeypatch):
    asset = FakePhotoAsset('already-added', is_live_photo=True)
    pipe, session, _, _ = make_pipeline([asset])
    pipe.settings.delete_from_icloud = False
    calls = configure(pipe, session, [asset], monkeypatch)
    pipe.run('initial')
    assert len(calls[0]['files']) == 1  # one destination item, not both pair paths
    assert pipe.run('again').totals.downloaded == 0
    assert len(calls) == 1


def test_dry_run_does_not_create_album_state(make_pipeline, monkeypatch):
    from icloud_to_gphotos.albums import album_state_path
    asset = FakePhotoAsset('preview')
    pipe, session, _, _ = make_pipeline([asset], dry_run=True)
    calls = configure(pipe, session, [asset], monkeypatch)
    pipe.run('dry-albums')
    assert calls == []
    assert not album_state_path(pipe.settings, session.library).exists()


def test_mapping_rejects_invalid_or_short_keys(tmp_path):
    from icloud_to_gphotos.albums import AlbumStore
    with AlbumStore(tmp_path / 'albums.db') as store:
        for key in ['Trip', 'AUTO', 'AF1QipX']:
            with pytest.raises(ValueError):
                store.bind('album', key)


def test_album_argument_and_summary_at_subprocess_boundary(tmp_path, monkeypatch):
    import subprocess

    from icloud_to_gphotos import uploader
    directory = tmp_path / 'media'
    directory.mkdir()
    (directory / 'image.jpg').write_bytes(b'media')
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0,
            stdout='{"album":{"albumKeys":["AF1Qip-album"],"itemsAdded":1}}', stderr='')
    monkeypatch.setattr(uploader.subprocess, 'run', run)
    report = uploader.upload_directory(directory, binary=tmp_path / 'gotohp', threads=1,
                                       pair_live_photos=False, album='AF1Qip-album')
    assert calls[0][-2:] == ['--album', 'AF1Qip-album']
    assert report.album['itemsAdded'] == 1


def test_bind_command_validates_source_and_persists_mapping(settings, monkeypatch):
    import json

    from typer.testing import CliRunner

    from icloud_to_gphotos import cli
    session = SimpleNamespace(library=SimpleNamespace(
        zone_id={'zoneName': 'PrimarySync'}, albums=[SimpleNamespace(id='source')]))
    monkeypatch.setattr(cli, '_settings', lambda: settings)
    monkeypatch.setattr(cli, 'connect', lambda _: session)
    from pathlib import Path
    monkeypatch.setattr(cli, 'find_gotohp', lambda _: Path('fake-gotohp'))
    monkeypatch.setattr(cli, 'DestinationGuard', lambda *a: SimpleNamespace(identity='test-scope'))
    runner = CliRunner()
    result = runner.invoke(cli.app, ['bind-album', 'missing', 'AF1Qip-destination'])
    assert result.exit_code != 0
    result = runner.invoke(cli.app, ['bind-album', 'source', 'AF1Qip-destination'])
    assert result.exit_code == 0
    result = runner.invoke(cli.app, ['album-mappings'])
    assert json.loads(result.stdout) == [{'album_id': 'source', 'media_key': 'AF1Qip-destination'}]
