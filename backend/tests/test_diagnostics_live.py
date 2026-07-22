import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_CONFIG_ENCRYPTION_KEY"] = "test-key"
os.environ["JWT_SIGNING_KEY"] = "test-jwt"

from fastapi.testclient import TestClient

from app.api import routes
from app.db.session import SessionLocal
from app.main import app
from app.services.integrations import get_config, upsert_config


client = TestClient(app)


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def admin_token() -> str:
    response = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    if response.status_code == 200:
        return response.json()["access_token"]
    created = client.post("/api/setup/admin", json={"username": "admin", "password": "password123"})
    assert created.status_code == 200
    return created.json()["access_token"]


def set_mteam_enabled(enabled: bool) -> None:
    with SessionLocal() as db:
        row = get_config(db, "mteam")
        assert row is not None
        row.enabled = enabled
        db.commit()


def test_live_diagnostic_module_checks_current_state(monkeypatch):
    token = admin_token()
    with SessionLocal() as db:
        upsert_config(db, "mteam", {"api_key": "diagnostic-test-key"}, actor_user_id=1)
    set_mteam_enabled(False)

    class FakeMTeamAdapter:
        calls = 0

        def __init__(self, config):
            self.config = config

        def test_connection(self):
            type(self).calls += 1
            return {"success": True, "username": "diagnostic-user"}

    monkeypatch.setattr(routes, "MTeamAdapter", FakeMTeamAdapter)

    disabled = client.post("/api/diagnostics/modules/mteam/check", headers=auth_headers(token))
    assert disabled.status_code == 200
    assert disabled.json()["module"]["status"] == "disabled"
    assert disabled.json()["module"]["live"] is True
    assert FakeMTeamAdapter.calls == 0

    set_mteam_enabled(True)
    healthy = client.post("/api/diagnostics/modules/mteam/check", headers=auth_headers(token))
    assert healthy.status_code == 200
    assert healthy.json()["module"]["status"] == "success"
    assert healthy.json()["module"]["checked_at"]
    assert FakeMTeamAdapter.calls == 1

    class FailingMTeamAdapter:
        def __init__(self, config):
            self.config = config

        def test_connection(self):
            raise RuntimeError("M-Team live probe unavailable")

    monkeypatch.setattr(routes, "MTeamAdapter", FailingMTeamAdapter)
    unhealthy = client.post("/api/diagnostics/modules/mteam/check", headers=auth_headers(token))
    assert unhealthy.status_code == 200
    assert unhealthy.json()["module"]["status"] == "failed"
    assert "live probe unavailable" in unhealthy.json()["module"]["last_error"]


def test_live_diagnostic_module_is_independent_and_validated(monkeypatch):
    token = admin_token()
    monkeypatch.setattr(
        routes,
        "storage_status_from_setup_check",
        lambda _db, refresh=False: {
            "nas_storage_readable": True,
            "nas_storage_errors": [],
            "nas_storage_check_source": "live" if refresh else "saved",
        },
    )

    storage = client.post("/api/diagnostics/modules/nas_disk/check", headers=auth_headers(token))
    assert storage.status_code == 200
    assert storage.json()["module"]["status"] == "success"
    assert storage.json()["module"]["message"] == "已实时确认 NAS 存储路径可以访问。"

    unknown = client.post("/api/diagnostics/modules/not-a-module/check", headers=auth_headers(token))
    assert unknown.status_code == 404
