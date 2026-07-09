import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_CONFIG_ENCRYPTION_KEY"] = "test-key"
os.environ["JWT_SIGNING_KEY"] = "test-jwt"

from fastapi.testclient import TestClient

from app.adapters.ai.client import render_mobile_reply
from app.api.routes import WechatClawMessageRequest, record_wechat_claw_interaction
from app.db.session import SessionLocal
from app.main import app


client = TestClient(app)


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def admin_token() -> str:
    login = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    if login.status_code == 200:
        return login.json()["access_token"]
    created = client.post("/api/setup/admin", json={"username": "admin", "password": "password123"})
    if created.status_code == 200:
        return created.json()["access_token"]
    login = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    assert login.status_code == 200
    return login.json()["access_token"]


def test_wechat_claw_provider_and_notification_preferences():
    token = admin_token()

    integrations = client.get("/api/admin/integrations", headers=auth_headers(token))
    assert integrations.status_code == 200
    assert "wechat_claw" in {item["provider"] for item in integrations.json()["providers"]}

    tested = client.post(
        "/api/admin/integrations/wechat_claw/test",
        headers=auth_headers(token),
        json={
            "payload": {
                "mode": "ilink",
                "name": "通知1",
                "base_url": "https://ilinkai.weixin.qq.com",
                "default_target": "wx-user-1",
                "admin_user_ids": "wx-user-1",
                "poll_timeout": 25,
                "default_downloader_id": "all",
            }
        },
    )
    assert tested.status_code == 200
    assert tested.json()["last_test_result"]["success"] is True

    enabled = client.post("/api/admin/integrations/wechat_claw/enable", headers=auth_headers(token))
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True

    setup = client.get("/api/admin/wechat-claw/setup", headers=auth_headers(token))
    assert setup.status_code == 200
    setup_body = setup.json()
    assert setup_body["mode"] == "ilink"
    assert setup_body["base_url"] == "https://ilinkai.weixin.qq.com"
    assert setup_body["qr_payload"]["mode"] == "ilink"
    assert setup_body["qr_payload"]["default_target"] == "wx-user-1"
    assert "settings" in {item["key"] for item in setup_body["mobile_sections"]}
    assert "status_query" in {item["action"] for item in setup_body["capabilities"]}
    assert setup_body["recent_interactions"] == []

    with SessionLocal() as db:
        record_wechat_claw_interaction(
            db,
            WechatClawMessageRequest(message="查一下仪表盘状态", user_id="wx-user-1", conversation_id="conv-1"),
            {"action": "status_query", "target": "dashboard"},
            {"target": "dashboard"},
            "仪表盘状态正常。",
        )

    setup_with_interaction = client.get("/api/admin/wechat-claw/setup", headers=auth_headers(token))
    assert setup_with_interaction.status_code == 200
    interaction = setup_with_interaction.json()["recent_interactions"][0]
    assert interaction["user_id"] == "wx-user-1"
    assert interaction["action"] == "status_query"
    assert interaction["target"] == "dashboard"

    preferences = client.put(
        "/api/notification-preferences",
        headers=auth_headers(token),
        json={"preferences": {"download_started": True, "download_completed": False, "wechat_claw_push": True}},
    )
    assert preferences.status_code == 200
    assert preferences.json()["preferences"]["download_completed"] is False

    skipped = client.post(
        "/api/assistant/execute",
        headers=auth_headers(token),
        json={"message": "测试完成通知", "intent": {"action": "download_completed", "message": "测试完成通知"}},
    )
    assert skipped.status_code == 200
    assert skipped.json()["result"]["notification_skipped"] is True
    assert "跳过" in skipped.json()["reply"]


def test_mobile_reply_renderer_has_stable_sections():
    rendered = render_mobile_reply(
        {
            "title": "资源查询",
            "summary": "找到 2 个候选资源。",
            "sections": [{"heading": "结果", "items": ["沙丘 / 2160p / 80GB / ID 123"]}],
            "actions": ["复制 ID 后可继续让助手下载。"],
            "footer": "数据来自 M-Team。",
        }
    )

    assert rendered.splitlines()[0] == "【资源查询】"
    assert "结果" in rendered
    assert "- 沙丘 / 2160p / 80GB / ID 123" in rendered
    assert "下一步" in rendered


def test_status_query_preference_records_notification():
    token = admin_token()

    preferences = client.put(
        "/api/notification-preferences",
        headers=auth_headers(token),
        json={"preferences": {"status_query": True, "wechat_claw_push": False}},
    )
    assert preferences.status_code == 200

    response = client.post(
        "/api/assistant/execute",
        headers=auth_headers(token),
        json={"message": "查一下统计状态", "intent": {"action": "status_query", "target": "stats"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["notification"]["title"] == "状态查询：统计"
    assert body["result"]["wechat_claw_push"]["reason"] == "disabled_by_preferences"
