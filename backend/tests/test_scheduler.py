import os
from types import SimpleNamespace

import pytest


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("APP_CONFIG_ENCRYPTION_KEY", "test-key")
os.environ.setdefault("JWT_SIGNING_KEY", "test-jwt")

from app.api import routes
from app.models.entities import MTeamSnapshot, QbSnapshot
from app.tasks import scheduler
from app.utils.time import utc_iso


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def add(self, value: object) -> None:
        self.added.append(value)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def install_snapshot_route_stubs(monkeypatch, placeholder_calls: list[tuple]) -> list[dict]:
    refreshed: list[dict] = []

    def placeholder(db, downloader_id, row, message=None):
        placeholder_calls.append((db, downloader_id, row, message))
        return {
            "id": downloader_id,
            "configured": bool(row and row.encrypted_payload),
            "enabled": bool(row and row.enabled),
            "online": False,
            "message": message or "not configured",
        }

    monkeypatch.setattr(routes, "qb_placeholder_state", placeholder)
    monkeypatch.setattr(routes, "record_module_collection_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "record_qb_task_transitions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "compact_mteam_snapshots", lambda _db: 0)
    monkeypatch.setattr(routes, "cleanup_debug_traces", lambda _db: 0)
    monkeypatch.setattr(routes, "cleanup_expired_user_sessions", lambda _db: 0)
    monkeypatch.setattr(routes, "refresh_collected_preload_caches", lambda _db, **payload: refreshed.append(payload))
    return refreshed


class WorkingQbAdapter:
    def __init__(self, _config):
        pass

    def get_server_state(self, downloader_id):
        return {
            "id": downloader_id,
            "online": True,
            "download_speed": 10,
            "upload_speed": 20,
            "downloaded_total": 30,
            "uploaded_total": 40,
            "active_downloads": 1,
            "active_uploads": 2,
            "captured_at": "2026-07-21T00:00:00Z",
            "checked_at": "2026-07-21T00:00:00Z",
            "stale": False,
            "updated_at": "2026-07-21T00:00:00Z",
        }

    def get_torrents(self, _downloader_id):
        return []

    def summarize_torrents(self, tasks):
        return {"task_count": len(tasks)}


@pytest.mark.parametrize(
    "enabled_ids",
    [set(), {"qb1"}, {"qb1", "qb2", "qb3"}],
    ids=["no-downloaders", "one-downloader", "three-downloaders"],
)
def test_capture_snapshots_handles_optional_downloader_counts(monkeypatch, enabled_ids):
    db = FakeSession()
    configured = SimpleNamespace(enabled=True, encrypted_payload="saved")
    rows = {downloader_id: configured for downloader_id in enabled_ids}
    placeholder_calls: list[tuple] = []
    refreshed = install_snapshot_route_stubs(monkeypatch, placeholder_calls)

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(scheduler, "get_config", lambda _db, provider: rows.get(provider))
    monkeypatch.setattr(scheduler, "get_decrypted_config", lambda _db, _provider: {})
    monkeypatch.setattr(scheduler, "QbittorrentWebAdapter", WorkingQbAdapter)

    scheduler.capture_snapshots()

    expected_placeholders = [item for item in ("qb1", "qb2", "qb3") if item not in enabled_ids]
    assert [call[1] for call in placeholder_calls] == expected_placeholders
    assert all(call[0] is db for call in placeholder_calls)
    assert len([item for item in db.added if isinstance(item, QbSnapshot)]) == len(enabled_ids)
    assert db.commits == 1
    assert db.rollbacks == 0
    assert db.closed is True
    assert len(refreshed) == 1
    assert set(refreshed[0]["downloads"]) == enabled_ids
    assert len(refreshed[0]["qbs"]) == 3


def test_capture_snapshots_keeps_mteam_data_when_a_downloader_fails(monkeypatch):
    db = FakeSession()
    configured = SimpleNamespace(enabled=True, encrypted_payload="saved")
    rows = {"mteam": configured, "qb1": configured}
    placeholder_calls: list[tuple] = []
    refreshed = install_snapshot_route_stubs(monkeypatch, placeholder_calls)

    def collect_mteam_snapshot(db):
        db.add(
            MTeamSnapshot(
                user_level="User",
                upload_total=100,
                download_total=50,
                bonus=10,
                ratio=2,
                seed_size=25,
                active_uploads=1,
                active_downloads=0,
                source="real",
            )
        )
        return {
            "user_level": "User",
            "upload_total": 100,
            "download_total": 50,
            "bonus": 10,
            "ratio": 2,
            "seed_size": 25,
            "active_uploads": 1,
            "active_downloads": 0,
            "captured_at": "2026-07-21T00:00:00Z",
            "checked_at": "2026-07-21T00:00:00Z",
            "stale": False,
        }

    class FailingQbAdapter:
        def __init__(self, _config):
            pass

        def get_server_state(self, _downloader_id):
            raise RuntimeError("qB is offline")

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db)
    monkeypatch.setattr(scheduler, "get_config", lambda _db, provider: rows.get(provider))
    monkeypatch.setattr(scheduler, "get_decrypted_config", lambda _db, _provider: {})
    monkeypatch.setattr(routes, "collect_mteam_snapshot", collect_mteam_snapshot)
    monkeypatch.setattr(scheduler, "QbittorrentWebAdapter", FailingQbAdapter)

    scheduler.capture_snapshots()

    assert any(isinstance(item, MTeamSnapshot) for item in db.added)
    assert db.rollbacks == 0
    assert db.commits == 1
    assert [call[1] for call in placeholder_calls] == ["qb1", "qb2", "qb3"]
    assert placeholder_calls[0][0] is db
    assert placeholder_calls[0][3] == "qB is offline"
    assert refreshed[0]["mteam"]["upload_total"] == 100
    assert len(refreshed[0]["qbs"]) == 3


def test_background_collection_retries_after_failure_without_user_sessions(monkeypatch):
    sessions = [FakeSession(), FakeSession()]
    configured = SimpleNamespace(enabled=True, encrypted_payload="saved")
    placeholder_calls: list[tuple] = []
    refreshed = install_snapshot_route_stubs(monkeypatch, placeholder_calls)
    attempts = 0

    def collect_mteam_snapshot(db):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary M-Team outage")
        snapshot = MTeamSnapshot(
            user_level="User",
            upload_total=200,
            download_total=80,
            source="real",
        )
        db.add(snapshot)
        return {
            "user_level": "User",
            "upload_total": 200,
            "download_total": 80,
            "captured_at": "2026-07-21T01:00:00Z",
            "checked_at": "2026-07-21T01:00:00Z",
            "stale": False,
        }

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: sessions.pop(0))
    monkeypatch.setattr(scheduler, "get_config", lambda _db, provider: configured if provider == "mteam" else None)
    monkeypatch.setattr(routes, "collect_mteam_snapshot", collect_mteam_snapshot)

    scheduler.capture_snapshots()
    scheduler.capture_snapshots()

    assert attempts == 2
    assert refreshed[0]["mteam"] is None
    assert refreshed[0]["mteam_error"] == "temporary M-Team outage"
    assert refreshed[1]["mteam"]["upload_total"] == 200
    assert refreshed[1]["mteam_error"] is None


def test_failed_qb_check_keeps_last_successful_capture_time():
    previous = [{
        "id": "qb1",
        "configured": True,
        "enabled": True,
        "online": True,
        "download_speed": 100,
        "captured_at": "2026-07-21T00:00:00Z",
        "checked_at": "2026-07-21T00:00:00Z",
        "stale": False,
    }]
    failed = [{
        "id": "qb1",
        "configured": True,
        "enabled": True,
        "online": False,
        "message": "offline",
        "captured_at": None,
        "checked_at": "2026-07-21T00:05:00Z",
        "stale": False,
    }]

    merged = routes.merge_qb_collection_with_previous(previous, failed)

    assert merged[0]["download_speed"] == 100
    assert merged[0]["captured_at"] == "2026-07-21T00:00:00Z"
    assert merged[0]["checked_at"] == "2026-07-21T00:05:00Z"
    assert merged[0]["online"] is False
    assert merged[0]["stale"] is True


def test_collect_mteam_snapshot_uses_the_external_capture_time(monkeypatch):
    db = FakeSession()

    class Adapter:
        def get_user_stats(self):
            return {
                "user_level": "User",
                "upload_total": 100,
                "download_total": 50,
                "captured_at": "2026-07-21T02:03:04Z",
            }

    monkeypatch.setattr(routes, "get_mteam_adapter_or_error", lambda _db: Adapter())

    result = routes.collect_mteam_snapshot(db)
    snapshot = next(item for item in db.added if isinstance(item, MTeamSnapshot))

    assert result["captured_at"] == "2026-07-21T02:03:04Z"
    assert result["updated_at"] == result["captured_at"]
    assert utc_iso(snapshot.captured_at) == "2026-07-21T02:03:04Z"


def test_wechat_poll_loop_waits_when_no_members_exist(monkeypatch):
    sessions: list[FakeSession] = []
    binding_queries = 0

    class ControlledEvent:
        def __init__(self):
            self.stopped = False
            self.waits: list[float] = []

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            self.waits.append(seconds)
            self.stopped = True
            return True

    event = ControlledEvent()

    def session_factory():
        session = FakeSession()
        sessions.append(session)
        return session

    def no_bindings(_db):
        nonlocal binding_queries
        binding_queries += 1
        if binding_queries > 1:
            raise RuntimeError("poll loop queried bindings again without waiting")
        return []

    monkeypatch.setattr(scheduler, "SessionLocal", session_factory)
    monkeypatch.setattr(scheduler, "_wechat_claw_stop_event", event)
    monkeypatch.setattr(routes, "list_wechat_claw_binding_user_ids", no_bindings)

    scheduler._wechat_claw_poll_loop()

    assert binding_queries == 1
    assert event.waits == [5.0]
    assert len(sessions) == 1
    assert sessions[0].closed is True
