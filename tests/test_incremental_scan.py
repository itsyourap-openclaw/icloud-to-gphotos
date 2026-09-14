# ruff: noqa: F811
"""A cursor checkpoint must never discard unfinished migration work."""

from types import SimpleNamespace

from freezegun import freeze_time

from .conftest import FakePhotoAsset, days_ago
from .test_pipeline import FakeGotohp, make_pipeline  # noqa: F401


class DeltaLibrary:
    zone_id = {"zoneName": "PrimarySync"}

    def __init__(self, session):
        self.current_sync_token = "initial"
        self.session = session
        self.events = []
        self.change_calls = []
        self.lookup_calls = []
        self.error = None
        self.all = SimpleNamespace(get=self.get)

    def sync_cursor(self):
        return self.current_sync_token or "fresh"

    def iter_changes(self, *, since):
        self.change_calls.append(since)
        # pyicloud advances this property before yielding records from a page.
        self.current_sync_token = "next"
        yield from self.events
        if self.error:
            raise self.error

    def get(self, asset_id):
        self.lookup_calls.append(asset_id)
        return next(
            (a for a in self.session._assets if a.id == asset_id and not a.delete_calls), None
        )


def enable(pipe, session):
    pipe.settings.incremental_scan = True
    session.library = DeltaLibrary(session)
    return session.library


def event(asset_id, record_type="CPLAsset", deleted=False):
    return SimpleNamespace(record_name=asset_id, record_type=record_type, deleted=deleted)


def test_bootstrap_then_empty_delta_avoids_full_listing(make_pipeline):
    asset = FakePhotoAsset("initial")
    pipe, session, _, _ = make_pipeline([asset])
    library = enable(pipe, session)
    assert pipe.run("bootstrap").totals.purged_assets == 1
    result = pipe.run("delta")
    assert session.iterations == 1
    assert library.change_calls == ["initial"]
    assert result.totals.scanned == 0
    assert result.scan["mode"] == "delta"


def test_failed_upload_retried_without_new_events(make_pipeline):
    asset = FakePhotoAsset("retry")
    fake = FakeGotohp(fail={asset.filename})
    pipe, session, _, _ = make_pipeline([asset], fake)
    library = enable(pipe, session)
    assert pipe.run("failed").totals.purged_assets == 0
    fake.fail.clear()
    result = pipe.run("retry-no-events")
    assert result.totals.purged_assets == 1
    assert session.iterations == 1 and library.lookup_calls == [asset.id]
    assert result.scan["pending"] == 0


def test_recent_confirmed_asset_stays_queued_until_eligible(make_pipeline):
    with freeze_time("2030-01-01"):
        asset = FakePhotoAsset("recent-import")
        asset.added_date = days_ago(0)
        pipe, session, _, _ = make_pipeline([asset])
        enable(pipe, session)
        pipe.settings.full_scan_interval_hours = 1000
        assert pipe.run("recent").scan["pending"] == 1
    with freeze_time("2030-01-09"):
        assert pipe.run("matured").totals.purged_assets == 1
    assert session.iterations == 1


def test_interrupted_change_page_keeps_old_cursor_and_queued_ids(make_pipeline):
    from icloud_to_gphotos.scan import ScanStore, scan_state_path

    pipe, session, _, _ = make_pipeline([])
    library = enable(pipe, session)
    pipe.run("bootstrap")
    library.events = [event("new")]
    library.error = RuntimeError("page interrupted")
    result = pipe.run("interrupted-page")
    assert result.status == "error"
    with ScanStore(scan_state_path(pipe.settings, library)) as store:
        assert store.cursor == "initial"
        assert list(store.pending_ids()) == ["new"]


def test_batch_limit_cannot_checkpoint_an_incomplete_full_scan(make_pipeline):
    from icloud_to_gphotos.scan import ScanStore, scan_state_path

    assets = [
        FakePhotoAsset("one", filename="one.HEIC"),
        FakePhotoAsset("two", filename="two.HEIC"),
    ]
    pipe, session, _, _ = make_pipeline(assets)
    library = enable(pipe, session)
    pipe.settings.batch_max_items = 1
    pipe.settings.max_batches_per_run = 1
    pipe.run("partial-full")
    with ScanStore(scan_state_path(pipe.settings, library)) as store:
        assert store.cursor is None


def test_expired_cursor_falls_back_to_full_scan(make_pipeline):
    pipe, session, _, _ = make_pipeline([])
    library = enable(pipe, session)
    pipe.run("bootstrap")
    library.error = RuntimeError("CHANGE_TOKEN_EXPIRED")
    result = pipe.run("expired")
    assert result.status == "ok"
    assert session.iterations == 2
    assert result.scan["mode"] == "full"


def test_unknown_master_or_album_event_triggers_reconciliation(make_pipeline):
    pipe, session, _, _ = make_pipeline([])
    library = enable(pipe, session)
    pipe.run("bootstrap")
    library.events = [event("unknown-master", "CPLMaster")]
    pipe.run("master-change")
    assert session.iterations == 2
    library.events = [event("album", "CPLAlbum")]
    pipe.run("album-change")
    assert session.iterations == 3


def test_missing_hydration_keeps_work_queued(make_pipeline):
    pipe, session, _, _ = make_pipeline([])
    library = enable(pipe, session)
    pipe.run("bootstrap")
    library.events = [event("not-ready")]
    result = pipe.run("not-hydrated")
    assert result.status == "partial"
    assert result.scan["pending"] == 1


def test_dry_run_never_writes_scan_state(make_pipeline):
    from icloud_to_gphotos.scan import scan_state_path

    pipe, session, _, _ = make_pipeline([FakePhotoAsset("preview")], dry_run=True)
    library = enable(pipe, session)
    pipe.run("preview")
    assert not scan_state_path(pipe.settings, library).exists()
    assert library.change_calls == []
