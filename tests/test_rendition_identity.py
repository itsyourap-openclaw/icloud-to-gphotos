# ruff: noqa: F811
"""An old same-size rendition is not proof of a current checksumless render."""

from .conftest import DEFAULT_PAYLOAD, FakePhotoAsset
from .test_pipeline import make_pipeline  # noqa: F401


def test_changed_checksumless_render_is_reuploaded_before_deletion(make_pipeline):
    asset = FakePhotoAsset('changed-render', adjustment_type='portrait',
                           edited_size=len(DEFAULT_PAYLOAD))
    pipe, _, uploader, ledger = make_pipeline([asset])
    pipe.settings.delete_from_icloud = False
    assert pipe.run('before-edit').totals.uploaded == 2
    asset._asset_record['recordChangeTag'] = 'new-revision'
    asset._service.session.payloads['https://cloudkit.invalid/edited'] = b'new-render-data!'
    pipe.settings.delete_from_icloud = True
    uploader.fail.add('IMG_0001_edited.JPG')
    result = pipe.run('changed-render-rejected')
    assert result.totals.downloaded == 1
    assert result.totals.purged_assets == 0
    assert ledger.get_resource(asset.id, 'original').is_uploaded
    uploader.fail.clear()
    result = pipe.run('changed-render-confirmed')
    assert result.totals.downloaded == 1
    assert result.totals.purged_assets == 1


def test_uncertain_resource_keeps_failure_budget_and_known_resource_keeps_proof(make_pipeline):
    from icloud_to_gphotos.ledger import MAX_UPLOAD_ATTEMPTS

    asset = FakePhotoAsset('retry-budget')
    asset.resources['original'].checksum = None
    pipe, _, uploader, ledger = make_pipeline([asset])
    uploader.fail.add(asset.filename)
    for number in range(MAX_UPLOAD_ATTEMPTS + 1):
        pipe.run(str(number))
    row = ledger.get_resource(asset.id, 'original')
    assert row.is_exhausted and row.attempts == MAX_UPLOAD_ATTEMPTS

    asset.resources['original'].checksum = 'reliable-new-fingerprint'
    uploader.fail.clear()
    pipe.settings.delete_from_icloud = False
    assert pipe.run('known').totals.uploaded == 1
    assert pipe.run('known-unchanged').totals.downloaded == 0
