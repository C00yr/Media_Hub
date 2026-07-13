import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_CONFIG_ENCRYPTION_KEY"] = "test-key"
os.environ["JWT_SIGNING_KEY"] = "test-jwt"

from fastapi.testclient import TestClient

from app.api import routes
from app.auth.security import hash_password
from app.db.session import SessionLocal
from app.main import app
from app.models.entities import Setting


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
    assert status.json() == {"initialized": True, "default_credentials_pending": True}

    blocked = client.post("/api/setup/admin", json={"username": "admin2", "password": "password123"})
    assert blocked.status_code == 409

    default_login = client.post("/api/auth/login", json={"username": "admin", "password": "adminadmin"})
    assert default_login.status_code == 200
    assert default_login.json()["user"]["must_change_credentials"] is True
    token = default_login.json()["access_token"]

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

    changed = client.put(
        "/api/auth/account",
        headers=auth_headers(token),
        json={"username": "admin", "current_password": "adminadmin", "new_password": "password123"},
    )
    assert changed.status_code == 200
    assert changed.json()["user"]["must_change_credentials"] is False
    token = changed.json()["access_token"]
    assert client.get("/api/auth/me", headers=auth_headers(default_login.json()["access_token"])).status_code == 401
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
