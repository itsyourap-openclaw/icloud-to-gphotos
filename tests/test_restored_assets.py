# ruff: noqa: F811
"""Reappearing source IDs must not inherit historical deletion completion."""

from .conftest import FakePhotoAsset
from .test_incremental_scan import enable, event
from .test_pipeline import make_pipeline  # noqa: F401


def test_restored_asset_failure_stays_queued_until_current_upload_succeeds(make_pipeline):
    asset = FakePhotoAsset('restored')
    pipe, session, uploader, ledger = make_pipeline([asset])
    library = enable(pipe, session)
    assert pipe.run('initial').totals.purged_assets == 1
    asset.delete_calls = 0
    asset.resources['original'].checksum = 'changed-after-restore'
    library.events = [event(asset.id)]
    uploader.fail.add(asset.filename)
    failed = pipe.run('restore-failed')
    assert failed.totals.failed == 1
    assert ledger.get_asset(asset.id).purged_at is None
    assert failed.scan['pending'] == 1
    library.events = []
    uploader.fail.clear()
    retried = pipe.run('restore-retry')
    assert retried.totals.uploaded == 1
    assert retried.totals.purged_assets == 1
    assert retried.scan['pending'] == 0


def test_unchanged_restored_asset_revalidates_upload_and_current_master(make_pipeline):
    asset = FakePhotoAsset('same-bytes-restored')
    pipe, _, _, ledger = make_pipeline([asset])
    assert pipe.run('initial').totals.purged_assets == 1
    asset.delete_calls = 0
    asset.master_id = 'replacement-master'
    pipe.settings.delete_from_icloud = False
    assert pipe.run('restored').totals.uploaded == 1
    assert ledger.get_asset(asset.id).purged_at is None
    assert ledger.asset_ids_for_master('replacement-master') == [asset.id]
    assert pipe.run('unchanged-backup').totals.downloaded == 0


def test_dry_run_does_not_reset_purged_history(make_pipeline):
    asset = FakePhotoAsset('preview-restored')
    pipe, _, _, ledger = make_pipeline([asset])
    pipe.run('initial')
    previous = ledger.get_asset(asset.id).purged_at
    asset.delete_calls = 0
    pipe.dry_run = True
    pipe.run('preview')
    assert ledger.get_asset(asset.id).purged_at == previous
    assert ledger.get_resource(asset.id, 'original').state == 'purged'
