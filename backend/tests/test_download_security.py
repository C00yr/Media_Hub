import hashlib
import os
from datetime import timedelta

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_CONFIG_ENCRYPTION_KEY"] = "test-key"
os.environ["JWT_SIGNING_KEY"] = "test-jwt"

from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from app.adapters.qbittorrent import QbittorrentApiError
from app.api import routes
from app.auth.security import DEFAULT_CREDENTIALS_PENDING_KEY, create_access_token, hash_password
from app.db.session import SessionLocal
from app.main import app
from app.models.entities import QbDeleteConfirmation, Setting, User
from app.utils.time import utc_now_naive


client = TestClient(app)


class FakeQbMutationAdapter:
    def __init__(self):
        self.calls = []

    def mutate_torrent(self, downloader_id, torrent_hash, action, payload):
        self.calls.append((downloader_id, torrent_hash, action, payload))
        return {"trace_id": "QB-ACTION-1", "accepted": True}


def authenticated_headers() -> dict[str, str]:
    with SessionLocal() as db:
        user = db.query(User).filter(User.role == "admin").order_by(User.id.asc()).first()
        if user is None:
            user = User(username="security-admin", password_hash=hash_password("password123"), role="admin")
            db.add(user)
            db.commit()
            db.refresh(user)
        pending = db.query(Setting).filter(Setting.key == DEFAULT_CREDENTIALS_PENDING_KEY).one_or_none()
        if pending is None:
            db.add(Setting(key=DEFAULT_CREDENTIALS_PENDING_KEY, value={"required": False}))
        else:
            pending.value = {"required": False}
        db.commit()
        token = create_access_token(db, user)
    return {"Authorization": f"Bearer {token}"}


def test_delete_confirmation_is_server_stored_bound_and_single_use(monkeypatch):
    adapter = FakeQbMutationAdapter()
    monkeypatch.setattr(routes, "get_qb_adapter_or_error", lambda _db, _downloader_id: adapter)
    headers = authenticated_headers()

    forged = client.delete(
        "/api/qb/qb1/torrents/hash-a?confirm_token=DEL-forged-token&delete_files=true",
        headers=headers,
    )
    assert forged.status_code == 400
    assert adapter.calls == []

    issued = client.post(
        "/api/qb/qb1/torrents/hash-a/delete-confirm?delete_files=true",
        headers=headers,
    )
    assert issued.status_code == 200
    token = issued.json()["confirm_token"]

    with SessionLocal() as db:
        stored = db.query(QbDeleteConfirmation).filter(
            QbDeleteConfirmation.token_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()
        ).one()
        assert token != stored.token_hash
        assert stored.downloader_id == "qb1"
        assert stored.torrent_hash == "hash-a"
        assert stored.delete_files is True

    wrong_target = client.delete(
        f"/api/qb/qb1/torrents/hash-b?confirm_token={token}&delete_files=true",
        headers=headers,
    )
    assert wrong_target.status_code == 400

    wrong_mode = client.delete(
        f"/api/qb/qb1/torrents/hash-a?confirm_token={token}&delete_files=false",
        headers=headers,
    )
    assert wrong_mode.status_code == 400

    deleted = client.delete(
        f"/api/qb/qb1/torrents/hash-a?confirm_token={token}&delete_files=true",
        headers=headers,
    )
    assert deleted.status_code == 200
    assert adapter.calls == [("qb1", "hash-a", "delete_files", {"delete_files": True})]

    replayed = client.delete(
        f"/api/qb/qb1/torrents/hash-a?confirm_token={token}&delete_files=true",
        headers=headers,
    )
    assert replayed.status_code == 400
    assert len(adapter.calls) == 1

    expired_issued = client.post(
        "/api/qb/qb1/torrents/hash-expired/delete-confirm",
        headers=headers,
    )
    assert expired_issued.status_code == 200
    expired_token = expired_issued.json()["confirm_token"]
    expired_hash = hashlib.sha256(expired_token.encode("utf-8")).hexdigest()
    with SessionLocal() as db:
        expired_confirmation = db.query(QbDeleteConfirmation).filter(
            QbDeleteConfirmation.token_hash == expired_hash
        ).one()
        expired_confirmation.expires_at = utc_now_naive() - timedelta(seconds=1)
        db.commit()
    expired_delete = client.delete(
        f"/api/qb/qb1/torrents/hash-expired?confirm_token={expired_token}",
        headers=headers,
    )
    assert expired_delete.status_code == 400
    assert len(adapter.calls) == 1


def test_download_overview_returns_last_successful_snapshot_as_stale(monkeypatch):
    captured_at = "2026-07-22T08:00:00Z"
    cached = {
        "payload": {
            "downloader_id": "qb1",
            "summary": {"captured_at": captured_at, "online": True},
            "items": [{"hash": "hash-a", "name": "Example"}],
            "captured_at": captured_at,
            "tasks_captured_at": captured_at,
            "stale": False,
        },
        "cached_at": "2026-07-22T08:00:01Z",
    }
    monkeypatch.setattr(routes, "get_preload_cache", lambda _db, _name: cached)
    monkeypatch.setattr(
        routes,
        "refresh_download_preload",
        lambda _db, _downloader_id: (_ for _ in ()).throw(QbittorrentApiError("offline")),
    )

    with SessionLocal() as db:
        result = routes.download_overview(
            "qb1", BackgroundTasks(), refresh=True, authorization=None, db=db, _=object()
        )

    assert result["stale"] is True
    assert result["captured_at"] == captured_at
    assert result["tasks_captured_at"] == captured_at
    assert result["summary"]["online"] is False
    assert result["summary"]["stale"] is True
    assert result["checked_at"] != captured_at


def test_qb2_summary_is_available_without_private_task_grant(monkeypatch):
    class FakeQbSummaryAdapter:
        def get_server_state(self, downloader_id):
            return {
                "id": downloader_id,
                "online": True,
                "download_speed": 1024,
                "upload_speed": 2048,
                "active_downloads": 2,
                "active_uploads": 3,
                "captured_at": "2026-07-22T08:00:00Z",
                "checked_at": "2026-07-22T08:00:00Z",
                "stale": False,
            }

    monkeypatch.setattr(
        routes, "get_qb_adapter_or_error", lambda _db, _downloader_id: FakeQbSummaryAdapter()
    )
    with SessionLocal() as db:
        result = routes.qb_summary("qb2", authorization=None, db=db, _=object())

    assert result["download_speed"] == 1024
    assert result["upload_speed"] == 2048
    assert result["active_downloads"] == 2
    assert result["active_uploads"] == 3
