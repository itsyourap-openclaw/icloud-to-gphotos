"""Selection is explicit, deduplicated, account-scoped, and backup-only outside root."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from icloud_to_gphotos.config import Settings
from icloud_to_gphotos.icloud_client import ICloudSession

from .conftest import FakePhotoAsset


def library(assets, albums):
    return SimpleNamespace(all=SimpleNamespace(photos=assets), albums=albums)


def album(key, path, assets):
    return SimpleNamespace(id=key, fullname=path, name=path.rsplit('/', 1)[-1], photos=assets)


def test_include_union_exclude_precedence_and_dedup():
    from icloud_to_gphotos.selection import select_assets
    a, b, c = [FakePhotoAsset(x) for x in 'abc']
    lib = library([a, b, c], [album('one', 'Travel/Trip', [a, b]),
                            album('two', 'Work/Trip', [b, c]), album('exclude', 'Private', [b])])
    assert [x.id for x in select_assets(lib, ['one', 'two'], ['exclude'])] == ['a', 'c']


def test_ambiguous_or_unknown_selector_fails_before_yield():
    from icloud_to_gphotos.selection import select_assets
    lib = library([], [album('one', 'Travel/Trip', []), album('two', 'Work/Trip', [])])
    for selector in ['Trip', 'missing']:
        with pytest.raises(ValueError):
            list(select_assets(lib, [selector], []))


def test_partial_exclusion_listing_yields_nothing():
    from icloud_to_gphotos.selection import select_assets
    def broken():
        yield FakePhotoAsset('a')
        raise RuntimeError('listing failed')
    lib = library([FakePhotoAsset('b')], [album('exclude', 'Private', broken())])
    with pytest.raises(RuntimeError):
        next(select_assets(lib, [], ['exclude']))


def test_shared_library_requires_backup_only(settings):
    values = settings.model_dump()
    values.update(icloud_library='shared:zone', delete_from_icloud=True)
    with pytest.raises(ValidationError, match='backup-only'):
        Settings(**values)
    values['delete_from_icloud'] = False
    assert Settings(**values).icloud_library == 'shared:zone'


def test_configured_library_is_exact_never_falls_back(settings):
    settings.icloud_library = 'shared:missing'
    api = SimpleNamespace(photos=SimpleNamespace(libraries={'root': 'personal'}))
    with pytest.raises(RuntimeError, match='not accessible'):
        _ = ICloudSession(api, settings=settings).library


def test_selected_ledgers_are_isolated_without_reusing_legacy_confirmations(tmp_path):
    paths = set()
    for account, key in [('a@example.invalid', 'root'), ('b@example.invalid', 'root'),
                         ('a@example.invalid', 'shared:zone')]:
        settings = Settings(icloud_username=account, state_dir=tmp_path,
                            icloud_library=key, delete_from_icloud=False, _env_file=None)
        paths.add(settings.ledger_path)
    assert len(paths) == 3
    assert tmp_path / 'ledger.db' not in paths


def test_shared_delete_is_rejected_even_if_caller_bypasses_config(settings):
    settings.icloud_library = 'shared:zone'
    asset = FakePhotoAsset('shared')
    with pytest.raises(RuntimeError, match='backup-only'):
        ICloudSession(SimpleNamespace(), settings=settings).delete_asset(asset)
    assert asset.delete_calls == 0


def test_source_zone_blocks_shared_delete_under_default_session():
    asset = FakePhotoAsset('shared-record')
    asset._asset_record['zoneID'] = {'zoneName': 'SharedSync-zone'}
    asset._asset_record['recordChangeTag'] = 'tag'
    with pytest.raises(RuntimeError, match='personal'):
        ICloudSession(SimpleNamespace()).delete_asset(asset)


def test_selected_album_does_not_include_hidden_or_deleted():
    from icloud_to_gphotos.selection import select_assets
    visible, hidden, deleted = [FakePhotoAsset(x) for x in ['visible', 'hidden', 'deleted']]
    hidden._asset_record['fields']['isHidden'] = {'value': 1}
    deleted._asset_record['fields']['isDeleted'] = {'value': 1}
    lib = library([], [album('one', 'Album', [visible, hidden, deleted])])
    assert [x.id for x in select_assets(lib, ['one'], [])] == ['visible']
