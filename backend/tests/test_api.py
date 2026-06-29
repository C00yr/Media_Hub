import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_CONFIG_ENCRYPTION_KEY"] = "test-key"
os.environ["JWT_SIGNING_KEY"] = "test-jwt"

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_setup_login_integration_and_diagnostics():
    status = client.get("/api/setup/status")
    assert status.status_code == 200

    created = client.post("/api/setup/admin", json={"username": "admin", "password": "password123"})
    assert created.status_code == 200
    token = created.json()["access_token"]

    blocked = client.post("/api/setup/admin", json={"username": "admin2", "password": "password123"})
    assert blocked.status_code == 409

    logged_in = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    assert logged_in.status_code == 200

    saved = client.put(
        "/api/admin/integrations/mteam",
        headers=auth_headers(token),
        json={"payload": {"raw_headers": "Cookie: secret1234\nUser-Agent: PTMH"}},
    )
    assert saved.status_code == 200
    assert saved.json()["redacted_summary"]["headers"]["Cookie"] == "saved ending 1234"

    tested = client.post("/api/admin/integrations/mteam/test", headers=auth_headers(token), json={"payload": {}})
    assert tested.status_code == 200
    assert tested.json()["last_test_result"]["success"] is True

    dashboard = client.get("/api/dashboard", headers=auth_headers(token))
    assert dashboard.status_code == 200

    export = client.post("/api/diagnostics/export", headers=auth_headers(token))
    assert export.status_code == 200
    payload_text = str(export.json()["payload"])
    assert "secret1234" not in payload_text

