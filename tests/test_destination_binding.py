# ruff: noqa: F811
"""Backup evidence belongs to one effective Google Photos destination."""

import subprocess

import pytest

from icloud_to_gphotos import pipeline
from icloud_to_gphotos.destination import DestinationGuard as RealDestinationGuard

from .conftest import FakePhotoAsset
from .test_incremental_scan import enable
from .test_pipeline import make_pipeline  # noqa: F401


def install_guard(monkeypatch):
    from icloud_to_gphotos import destination
    selected = ['a@example.invalid']
    monkeypatch.setattr(destination, 'selected_account', lambda *a, **kw: selected[0])
    monkeypatch.setattr(pipeline.destination_integration, 'DestinationGuard',
                        RealDestinationGuard)
    return selected


def test_switching_account_blocks_old_confirmation_and_leaves_source(make_pipeline, monkeypatch):
    asset = FakePhotoAsset('account-change')
    pipe, _, uploader, _ = make_pipeline([asset])
    selected = install_guard(monkeypatch)
    pipe.settings.delete_from_icloud = False
    assert pipe.run('account-a').totals.uploaded == 1
    selected[0] = 'b@example.invalid'
    pipe.settings.delete_from_icloud = True
    uploader.calls.clear()
    result = pipe.run('account-b')
    assert result.status == 'error' and not uploader.calls
    assert result.totals.purged_assets == 0 and asset.delete_calls == 0
    assert 'a@example.invalid' not in str(result.as_dict())
    selected[0] = 'a@example.invalid'
    assert pipe.run('original-destination').totals.purged_assets == 1


def test_legacy_unbound_uploads_are_reconfirmed_with_full_discovery(make_pipeline, monkeypatch):
    asset = FakePhotoAsset('legacy')
    pipe, session, _, ledger = make_pipeline([asset])
    enable(pipe, session)
    pipe.settings.delete_from_icloud = False
    pipe.run('previous-version')
    # Model an upgrade from a ledger/scan produced before destination binding.
    ledger._conn.execute("DELETE FROM meta WHERE key LIKE 'destination_%'")
    install_guard(monkeypatch)
    result = pipe.run('upgrade')
    assert result.scan['mode'] == 'full' and result.totals.uploaded == 1
    assert pipe.run('same-account').scan['mode'] == 'delta'


def test_account_switch_during_upload_discards_verdicts(make_pipeline, monkeypatch):
    asset = FakePhotoAsset('mid-upload-switch')
    pipe, _, uploader, ledger = make_pipeline([asset])
    selected = install_guard(monkeypatch)
    def changed_during_upload(*args, **kwargs):
        result = uploader(*args, **kwargs)
        selected[0] = 'b@example.invalid'
        return result
    monkeypatch.setattr(pipeline, 'upload_directory', changed_during_upload)
    result = pipe.run('switch')
    assert result.status == 'error' and result.totals.purged_assets == 0
    assert not ledger.get_resource(asset.id, 'original').is_uploaded


def test_probe_requires_one_active_account_without_leaking_output(monkeypatch, tmp_path):
    from icloud_to_gphotos import destination
    from icloud_to_gphotos.uploader import UploadError
    output = ['Credentials:\n  a@example.invalid\n']
    monkeypatch.setattr(destination.subprocess, 'run', lambda *a, **kw:
                        subprocess.CompletedProcess([], 0, output[0], 'SYNTHETIC_SECRET'))
    with pytest.raises(UploadError) as error:
        destination.selected_account(tmp_path/'fake', None)
    assert 'SYNTHETIC_SECRET' not in str(error.value)
    output[0] = 'Credentials:\n  * a@example.invalid\n\n* = active\n'
    assert destination.selected_account(tmp_path/'fake', None) == 'a@example.invalid'


def test_ignored_explicit_config_is_rejected(monkeypatch, tmp_path):
    from icloud_to_gphotos import destination
    from icloud_to_gphotos.uploader import UploadError
    monkeypatch.setattr(destination.subprocess, 'run', lambda *a, **kw:
                        subprocess.CompletedProcess([], 0, '  * a@example.invalid\n', ''))
    config = tmp_path/'explicit.config'
    config.write_text('selected: a@example.invalid\n')
    with pytest.raises(UploadError, match='ignores --config'):
        destination.DestinationGuard(tmp_path/'fake', config)


def test_honored_config_and_same_account_credential_refresh(monkeypatch, tmp_path):
    from icloud_to_gphotos import destination
    config = tmp_path/'account.config'
    config.write_text('selected: a@example.invalid\n')
    def probe(command, **kwargs):
        empty = command[-1].endswith('empty.config')
        return subprocess.CompletedProcess([], 0, '' if empty else '  * a@example.invalid\n', '')
    monkeypatch.setattr(destination.subprocess, 'run', probe)
    guard = RealDestinationGuard(tmp_path/'fake-gotohp', config)
    config.write_text('selected: a@example.invalid\ncredentials: [refreshed]\n')
    guard.check()
    assert guard.identity == destination.account_identity('A@example.invalid', config)


def test_ledger_binding_rejects_changed_config_and_preserves_deleted_history(tmp_path):
    from icloud_to_gphotos.destination import account_identity
    from icloud_to_gphotos.ledger import DestinationMismatch, Ledger
    from tests.test_ledger import _add_asset, _add_resource

    with Ledger(tmp_path/'ledger.db') as ledger:
        for asset in ('retained', 'deleted'):
            _add_asset(ledger, asset, asset)
            _add_resource(ledger, asset, 'original', f'{asset}.HEIC')
            ledger.mark_uploaded(asset, 'original', 'old-key')
        ledger.mark_asset_purged('deleted')
        identity = account_identity('a@example.invalid', tmp_path/'a.config')
        assert ledger.bind_destination(identity)
        assert ledger.get_resource('retained', 'original').state == 'pending'
        assert ledger.get_resource('deleted', 'original').state == 'purged'
        with pytest.raises(DestinationMismatch):
            ledger.bind_destination(account_identity('a@example.invalid', tmp_path/'b.config'))
        assert ledger.destination_identity() == identity


def test_album_namespaces_do_not_reuse_legacy_or_other_account_mappings(settings):
    from types import SimpleNamespace

    from icloud_to_gphotos.albums import album_state_path
    from icloud_to_gphotos.destination import account_identity
    library = SimpleNamespace(zone_id={'zoneName': 'PrimarySync'})
    paths = {album_state_path(settings, library)}
    for account in ('a@example.invalid', 'b@example.invalid'):
        paths.add(album_state_path(settings, library, account_identity(account, None)))
    assert len(paths) == 3
