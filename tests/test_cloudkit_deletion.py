"""Validate deletion against real pyicloud CloudKit response models, not booleans."""

from types import SimpleNamespace

import pytest
from pyicloud.common.cloudkit import CKRecord
from pyicloud.common.cloudkit.models import CKModifyResponse
from pyicloud.services.photos_cloudkit.service import PhotoAsset

from icloud_to_gphotos.icloud_client import ICloudSession


def _asset(records, *, raw=False):
    calls = []

    def modify(**kwargs):
        calls.append(kwargs)
        return CKModifyResponse.model_validate({"records": records})

    record = {
        "recordName": "asset", "recordType": "CPLAsset", "recordChangeTag": "asset-tag",
        "zoneID": {"zoneName": "PrimarySync", "ownerRecordName": "owner"},
        "fields": {},
    }
    asset = PhotoAsset(
        SimpleNamespace(private_client=SimpleNamespace(modify=modify)),
        CKRecord(recordName="master", recordType="CPLMaster", recordChangeTag="master-tag"),
        record if raw else CKRecord.model_validate(record),
    )
    return asset, calls


@pytest.mark.parametrize("records", [
    [{"recordName": "asset", "serverErrorCode": "CONFLICT", "reason": "changed"}],
    [],
    [{"recordName": "other", "recordType": "CPLAsset", "fields": {"isDeleted": {"value": 1}}}],
    [{"recordName": "asset", "recordType": "CPLAsset", "fields": {"isDeleted": {"value": 0}}}],
    [{"recordName": "asset", "recordType": "CPLAsset", "fields": {}}],
])
def test_unacknowledged_delete_is_a_failure(records):
    asset, _ = _asset(records)
    with pytest.raises(RuntimeError):
        ICloudSession(SimpleNamespace()).delete_asset(asset)


@pytest.mark.parametrize("raw", [False, True])
def test_verified_soft_delete_uses_asset_tag_and_correct_zone(raw):
    asset, calls = _asset([{
        "recordName": "asset", "recordType": "CPLAsset",
        "fields": {"isDeleted": {"value": 1}},
    }], raw=raw)
    assert ICloudSession(SimpleNamespace()).delete_asset(asset)
    request = calls[0]
    assert request["atomic"] is True
    assert request["zone_id"].zoneName == "PrimarySync"
    operation = request["operations"][0]
    assert operation.operationType == "update"
    assert operation.record.recordChangeTag == "asset-tag"
    assert operation.record.fields == {"isDeleted": {"type": "INT64", "value": 1}}


def test_missing_asset_tag_never_uses_master_tag_or_force_update():
    asset, calls = _asset([])
    asset._asset_record.recordChangeTag = None
    with pytest.raises(RuntimeError, match="change tag"):
        ICloudSession(SimpleNamespace()).delete_asset(asset)
    assert calls == []
