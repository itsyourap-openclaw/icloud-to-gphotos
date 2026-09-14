"""Opt-in repair must prove a linked pair, including on upgrades and retries."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from icloud_to_gphotos import pipeline as pipeline_module
from icloud_to_gphotos.config import Settings
from icloud_to_gphotos.uploader import IncompatibleGotohp, parse_summary, verify_compatible

from .conftest import FakePhotoAsset
from .test_pipeline import make_pipeline  # noqa: F401
from .test_uploader_process import recorded_run, staged  # noqa: F401


def enable(pipe, monkeypatch):
    pipe.settings.update_existing_photos_to_live = True
    monkeypatch.setattr(pipeline_module, 'verify_compatible', lambda *a, **kw: None)


@pytest.mark.parametrize('overrides', [
    {'pair_live_photos': False}, {'include_live_photo_video': False}, {'edited_policy': 'edited'},
])
def test_repair_requires_original_pair(overrides):
    with pytest.raises(ValidationError):
        Settings(icloud_username='test@example.invalid', update_existing_photos_to_live=True,
                 _env_file=None, **overrides)


def test_repair_flag_and_strict_pairing(staged, recorded_run):
    from icloud_to_gphotos.uploader import upload_directory
    calls = recorded_run()
    upload_directory(staged, binary=Path('gotohp'), threads=1, pair_live_photos=True,
                     update_existing_photos_to_live=True)
    assert '--update-existing-photos-to-live' in calls[0]
    assert '--upload-incomplete-live-photos' not in calls[0]


def test_probe_rejects_missing_repair_capability(recorded_run):
    recorded_run(stdout='upload --no-tui --pair-live-photos --upload-incomplete-live-photos')
    with pytest.raises(IncompatibleGotohp, match='update-existing-photos-to-live'):
        verify_compatible(Path('gotohp'), update_existing_photos_to_live=True)


def test_existing_confirmations_are_reconciled_once(make_pipeline, monkeypatch):
    asset = FakePhotoAsset('existing-pair', is_live_photo=True)
    pipe, _, uploader, ledger = make_pipeline([asset])
    pipe.settings.delete_from_icloud = False
    pipe.run('before-repair')
    enable(pipe, monkeypatch)
    result = pipe.run('repair')
    assert result.totals.downloaded == 2
    assert result.totals.uploaded == 2
    calls = len(uploader.calls)
    pipe.run('already-repaired')
    assert len(uploader.calls) == calls
    pipe.settings.delete_from_icloud = True
    assert pipe.run('purge').totals.purged_assets == 1


def test_separate_successes_do_not_prove_linkage(make_pipeline, monkeypatch):
    asset = FakePhotoAsset('separate-success', is_live_photo=True)
    pipe, _, _, ledger = make_pipeline([asset])
    enable(pipe, monkeypatch)
    def standalone(directory, **kwargs):
        return parse_summary({'results': [
            {'path': str(p), 'success': True, 'mediaKey': p.name}
            for p in directory.rglob('*') if p.is_file()
        ]})
    monkeypatch.setattr(pipeline_module, 'upload_directory', standalone)
    result = pipe.run('standalone')
    assert result.totals.purged_assets == 0
    assert result.totals.failed == 2
    assert not ledger.asset_ready_to_purge(asset.id)


def test_repair_unsupported_direction_retains_source(make_pipeline, monkeypatch):
    asset = FakePhotoAsset('remote-video', is_live_photo=True)
    pipe, _, _, ledger = make_pipeline([asset])
    enable(pipe, monkeypatch)
    def skipped(directory, **kwargs):
        paths = [str(p) for p in directory.rglob('*') if p.is_file()]
        return parse_summary({'results': [{'paths': paths, 'skipped': True,
            'skipCode': 'remote-live-photo-component-exists'}]})
    monkeypatch.setattr(pipeline_module, 'upload_directory', skipped)
    assert pipe.run('unrepairable').totals.purged_assets == 0
    assert not ledger.asset_ready_to_purge(asset.id)


def test_dry_run_does_not_reset_existing_confirmations(make_pipeline, monkeypatch):
    asset = FakePhotoAsset('preview-repair', is_live_photo=True)
    pipe, _, _, ledger = make_pipeline([asset])
    pipe.settings.delete_from_icloud = False
    pipe.run('initial')
    before = ledger._conn.iterdump()
    saved = list(before)
    enable(pipe, monkeypatch)
    pipe.dry_run = True
    pipe.run('preview')
    assert list(ledger._conn.iterdump()) == saved
