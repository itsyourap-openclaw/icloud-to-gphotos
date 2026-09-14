# ruff: noqa: F811
"""Preservation policy changes are work even without new CloudKit events."""

import pytest

from icloud_to_gphotos.ledger import Ledger

from .conftest import FakePhotoAsset
from .test_incremental_scan import enable
from .test_pipeline import make_pipeline  # noqa: F401


def test_enabling_archive_reconciles_unchanged_backups(make_pipeline, tmp_path):
    pipe, session, _, _ = make_pipeline([FakePhotoAsset('archive-policy')])
    library = enable(pipe, session)
    library.albums = []
    pipe.settings.delete_from_icloud = False
    assert pipe.run('backup').scan['pending'] == 0
    pipe.settings.metadata_archive_dir = tmp_path/'archive'
    result = pipe.run('enable-archive')
    assert result.scan['mode'] == 'full' and result.totals.scanned == 1
    assert list(pipe.settings.metadata_archive_dir.rglob('*.json'))
    assert pipe.run('unchanged-policy').totals.scanned == 0


@pytest.mark.parametrize(('field', 'value'), [
    ('edited_policy', 'original'), ('include_live_photo_video', False),
    ('include_alternative_original', True), ('backfill_metadata', True),
    ('preserve_albums', True), ('pair_live_photos', False),
    ('update_existing_photos_to_live', True), ('ignore_apple_metadata', True),
    ('delete_from_icloud', False), ('delete_grace_days', 30),
])
def test_preservation_changes_force_full_discovery(make_pipeline, monkeypatch, field, value):
    pipe, session, _, _ = make_pipeline([])
    from icloud_to_gphotos import pipeline
    monkeypatch.setattr(pipeline, 'verify_compatible', lambda *a, **kw: None)
    enable(pipe, session)
    pipe.run('initial')
    setattr(pipe.settings, field, value)
    assert pipe.run('changed').scan['mode'] == 'full'


def test_new_ledger_cannot_reuse_old_discovery_checkpoint(make_pipeline, tmp_path):
    pipe, session, _, _ = make_pipeline([FakePhotoAsset('new-ledger')])
    enable(pipe, session)
    pipe.settings.delete_from_icloud = False
    pipe.run('initial')
    with Ledger(tmp_path/'replacement.db') as fresh:
        pipe.ledger = fresh
        result = pipe.run('fresh-ledger')
        assert result.scan['mode'] == 'full'
        assert result.totals.uploaded == 1


def test_incomplete_policy_scan_does_not_commit_new_policy(make_pipeline):
    assets = [FakePhotoAsset(f'asset-{n}', filename=f'{n}.HEIC') for n in range(3)]
    pipe, session, _, _ = make_pipeline(assets)
    enable(pipe, session)
    pipe.settings.delete_from_icloud = False
    pipe.run('initial')
    pipe.settings.delete_grace_days = 100
    # Force an interruption during discovery, independently of upload state.
    original = session.iter_all_assets
    def broken():
        yield assets[0]
        raise RuntimeError('page interrupted')
    session.iter_all_assets = broken
    assert pipe.run('interrupted-policy').status == 'error'
    session.iter_all_assets = original
    assert pipe.run('retry-policy').scan['mode'] == 'full'
    pipe.settings.download_workers = 2
    assert pipe.run('performance-only-change').scan['mode'] == 'delta'
