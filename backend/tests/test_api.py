import os
from datetime import timedelta

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_CONFIG_ENCRYPTION_KEY"] = "test-key"
os.environ["JWT_SIGNING_KEY"] = "test-jwt"

from fastapi.testclient import TestClient

from app.api import routes
from app.auth.security import decode_token, hash_password
from app.db.session import SessionLocal
from app.main import app, ensure_legacy_default_credentials_state
from app.models.entities import Setting, User, UserSession
from app.utils.time import utc_now_naive


client = TestClient(app)


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class FakeMTeamAdapter:
    def __init__(self, config):
        self.config = config

    def get_user_stats(self):
        return {
            "user_level": "User",
            "upload_total": 1024,
            "download_total": 512,
            "bonus": 100,
            "ratio": 2,
            "seed_count": 1,
            "seed_size": 1024,
            "joined_at": "2026-06-12",
            "traffic_history": [],
            "source": "M-Team 原始数据（Test）",
        }


def test_setup_login_integration_and_diagnostics(monkeypatch):
    monkeypatch.setattr(routes, "MTeamAdapter", FakeMTeamAdapter)
    status = client.get("/api/setup/status")
    assert status.status_code == 200
    assert status.json() == {"initialized": False, "default_credentials_pending": False}

    rejected = client.post("/api/setup/admin", json={"username": "admin", "password": "adminadmin"})
    assert rejected.status_code == 400

    created = client.post("/api/setup/admin", json={"username": "admin", "password": "initial-password123"})
    assert created.status_code == 200
    assert created.json()["user"]["must_change_credentials"] is False
    token = created.json()["access_token"]

    blocked = client.post("/api/setup/admin", json={"username": "admin2", "password": "password123"})
    assert blocked.status_code == 409

    db = SessionLocal()
    try:
        super_password = db.query(Setting).filter(Setting.key == "auth.super_password").one()
        super_password.value = {"password_hash": hash_password("test-recovery-password")}
        db.commit()
    finally:
        db.close()
    recovery_login = client.post("/api/auth/login", json={"username": "", "password": "test-recovery-password"})
    assert recovery_login.status_code == 200
    assert recovery_login.json()["user"]["username"] == "admin"

    rejected_legacy_password = client.put(
        "/api/auth/account",
        headers=auth_headers(token),
        json={"username": "admin", "current_password": "initial-password123", "new_password": "adminadmin"},
    )
    assert rejected_legacy_password.status_code == 400

    changed = client.put(
        "/api/auth/account",
        headers=auth_headers(recovery_login.json()["access_token"]),
        json={"username": "admin", "current_password": "test-recovery-password", "new_password": "password123"},
    )
    assert changed.status_code == 200
    assert changed.json()["user"]["must_change_credentials"] is False
    token = changed.json()["access_token"]
    assert client.get("/api/auth/me", headers=auth_headers(created.json()["access_token"])).status_code == 401
    assert client.post("/api/auth/login", json={"username": "anything", "password": "test-recovery-password"}).status_code == 200

    logged_in = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    assert logged_in.status_code == 200

    saved = client.put(
        "/api/admin/integrations/mteam",
        headers=auth_headers(token),
        json={"payload": {"api_key": "mteam-api-secret1234", "raw_headers": "Cookie: secret1234\nUser-Agent: PTMH"}},
    )
    assert saved.status_code == 200
    assert saved.json()["redacted_summary"]["headers"]["Cookie"] == "saved ending 1234"
    assert saved.json()["redacted_summary"]["api_key"] == "saved ending 1234"
    settings_payload = client.get("/api/admin/integrations", headers=auth_headers(token))
    mteam_settings = next(item for item in settings_payload.json()["providers"] if item["provider"] == "mteam")
    assert mteam_settings["saved_payload"]["api_key"] == "mteam-api-secret1234"
    assert mteam_settings["saved_payload"]["headers"]["Cookie"] == "secret1234"

    tested = client.post("/api/admin/integrations/mteam/test", headers=auth_headers(token), json={"payload": {}})
    assert tested.status_code == 200
    assert tested.json()["last_test_result"]["success"] is True

    dashboard = client.get("/api/dashboard", headers=auth_headers(token))
    assert dashboard.status_code == 200

    monkeypatch.setattr(routes, "nas_storage_from_configs", lambda _db: {"nas_storage_readable": False})
    health = client.get("/api/diagnostics/health", headers=auth_headers(token))
    assert health.status_code == 200
    modules = {item["module"]: item for item in health.json()["modules"]}
    assert modules["nas_disk"]["status"] == "failed"
    assert modules["nas_disk"]["last_error"] == "请在 YAML 中填写并挂载 NAS 文件夹路径。"
    assert "stats_engine" not in modules


def test_legacy_default_credentials_require_update_before_app_access():
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.role == "admin").one()
        admin.password_hash = hash_password("adminadmin")
        pending = db.query(Setting).filter(Setting.key == "account.default_credentials_pending").one()
        pending.value = {"required": False}
        db.commit()
    finally:
        db.close()

    ensure_legacy_default_credentials_state()
    assert client.get("/api/setup/status").json() == {"initialized": True, "default_credentials_pending": True}

    logged_in = client.post("/api/auth/login", json={"username": "admin", "password": "adminadmin"})
    assert logged_in.status_code == 200
    assert logged_in.json()["user"]["must_change_credentials"] is True
    legacy_token = logged_in.json()["access_token"]

    blocked = client.get("/api/admin/integrations", headers=auth_headers(legacy_token))
    assert blocked.status_code == 403
    assert client.get("/api/auth/me", headers=auth_headers(legacy_token)).status_code == 200

    changed = client.put(
        "/api/auth/account",
        headers=auth_headers(legacy_token),
        json={"username": "admin", "current_password": "adminadmin", "new_password": "password123"},
    )
    assert changed.status_code == 200
    assert changed.json()["user"]["must_change_credentials"] is False
    assert client.get("/api/admin/integrations", headers=auth_headers(changed.json()["access_token"])).status_code == 200

def test_ai_provider_and_structured_assistant_execute():
    logged_in = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    assert logged_in.status_code == 200
    token = logged_in.json()["access_token"]

    integrations = client.get("/api/admin/integrations", headers=auth_headers(token))
    assert integrations.status_code == 200
    assert "ai" in {item["provider"] for item in integrations.json()["providers"]}

    saved = client.put(
        "/api/admin/integrations/ai",
        headers=auth_headers(token),
        json={"payload": {"api_key": "deepseek-secret1234", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash"}},
    )
    assert saved.status_code == 200
    assert saved.json()["redacted_summary"]["api_key"] == "saved ending 1234"

    executed = client.post(
        "/api/assistant/execute",
        headers=auth_headers(token),
        json={"message": "测试下载完成", "intent": {"action": "download_completed", "message": "测试下载完成"}},
    )
    assert executed.status_code == 200
    body = executed.json()
    assert body["intent"]["action"] == "download_completed"
    assert body["result"]["monitoring"] is True


def test_downloader_targets_expose_webui_url_without_credentials():
    logged_in = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    assert logged_in.status_code == 200
    token = logged_in.json()["access_token"]

    saved = client.put(
        "/api/admin/integrations/qb1",
        headers=auth_headers(token),
        json={
            "payload": {
                "base_url": "http://192.168.1.20:8080/",
                "username": "qb-user",
                "password": "qb-password",
            }
        },
    )
    assert saved.status_code == 200

    response = client.get("/api/downloaders/targets", headers=auth_headers(token))
    assert response.status_code == 200
    target = next(item for item in response.json()["items"] if item["id"] == "qb1")
    assert target["configured"] is True
    assert target["webui_url"] == "http://192.168.1.20:8080"
    assert "username" not in target
    assert "password" not in target

    settings_payload = client.get("/api/admin/integrations", headers=auth_headers(token))
    qb_settings = next(item for item in settings_payload.json()["providers"] if item["provider"] == "qb1")
    assert qb_settings["saved_payload"]["password"] == "qb-password"


def test_login_sessions_logout_expiry_and_last_seen_throttle():
    first_login = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    second_login = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    assert first_login.status_code == 200
    assert second_login.status_code == 200
    first_token = first_login.json()["access_token"]
    second_token = second_login.json()["access_token"]
    first_token_id = decode_token(first_token)["jti"]
    second_token_id = decode_token(second_token)["jti"]

    db = SessionLocal()
    try:
        first_session = db.query(UserSession).filter(UserSession.token_id == first_token_id).one()
        second_session = db.query(UserSession).filter(UserSession.token_id == second_token_id).one()
        assert first_session.expires_at is not None
        assert second_session.expires_at is not None
        original_last_seen = first_session.last_seen_at
    finally:
        db.close()

    assert client.get("/api/auth/me", headers=auth_headers(first_token)).status_code == 200
    db = SessionLocal()
    try:
        first_session = db.query(UserSession).filter(UserSession.token_id == first_token_id).one()
        assert first_session.last_seen_at == original_last_seen
        first_session.last_seen_at = utc_now_naive() - timedelta(minutes=6)
        db.commit()
    finally:
        db.close()

    assert client.get("/api/auth/me", headers=auth_headers(first_token)).status_code == 200
    db = SessionLocal()
    try:
        first_session = db.query(UserSession).filter(UserSession.token_id == first_token_id).one()
        assert first_session.last_seen_at > utc_now_naive() - timedelta(minutes=1)
    finally:
        db.close()

    bad_login = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})
    assert bad_login.status_code == 401
    assert client.get("/api/auth/me", headers=auth_headers(second_token)).status_code == 200

    logged_out = client.post("/api/auth/logout", headers=auth_headers(first_token))
    assert logged_out.status_code == 200
    invalid = client.get("/api/auth/me", headers=auth_headers(first_token))
    assert invalid.status_code == 401
    assert invalid.json()["detail"]["code"] == "auth_session_invalid"
    assert client.get("/api/auth/me", headers=auth_headers(second_token)).status_code == 200

    db = SessionLocal()
    try:
        second_session = db.query(UserSession).filter(UserSession.token_id == second_token_id).one()
        second_session.expires_at = utc_now_naive() - timedelta(seconds=1)
        db.commit()
        assert routes.cleanup_expired_user_sessions(db) == 1
    finally:
        db.close()
    expired = client.get("/api/auth/me", headers=auth_headers(second_token))
    assert expired.status_code == 401
    assert expired.json()["detail"]["code"] == "auth_session_invalid"


def test_legacy_session_expiry_is_filled_from_jwt_on_first_use():
    logged_in = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    assert logged_in.status_code == 200
    token = logged_in.json()["access_token"]
    token_id = decode_token(token)["jti"]
    db = SessionLocal()
    try:
        session = db.query(UserSession).filter(UserSession.token_id == token_id).one()
        session.expires_at = None
        db.commit()
    finally:
        db.close()

    assert client.get("/api/auth/me", headers=auth_headers(token)).status_code == 200
    db = SessionLocal()
    try:
        assert db.query(UserSession).filter(UserSession.token_id == token_id).one().expires_at is not None
    finally:
        db.close()


def test_ai_status_queries_only_read_background_snapshots(monkeypatch):
    adapter_calls = 0

    class UnexpectedLiveAdapter:
        def __init__(self, _config):
            nonlocal adapter_calls
            adapter_calls += 1
            raise AssertionError("AI status queries must not contact qB directly")

    monkeypatch.setattr(routes, "QbittorrentWebAdapter", UnexpectedLiveAdapter)
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.role == "admin").one()
        all_downloaders = routes.execute_assistant_intent(
            db,
            {"action": "status_query", "target": "downloads", "downloader_id": "all"},
            "",
            admin,
        )
        one_downloader = routes.execute_assistant_intent(
            db,
            {"action": "status_query", "target": "downloads", "downloader_id": "qb1"},
            "",
            admin,
        )
    finally:
        db.close()

    assert adapter_calls == 0
    assert isinstance(all_downloaders["qbs"], list)
    assert one_downloader["qbs"][0]["id"] == "qb1"
