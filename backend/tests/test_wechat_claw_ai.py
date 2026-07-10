import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_CONFIG_ENCRYPTION_KEY"] = "test-key"
os.environ["JWT_SIGNING_KEY"] = "test-jwt"

from fastapi.testclient import TestClient

from app.adapters.ai.client import render_mobile_reply
from app.adapters.wechat_claw import WechatClawAdapter
from app.api.routes import (
    WechatClawMessageRequest,
    clear_wechat_claw_session,
    get_wechat_claw_ilink_state,
    get_wechat_claw_pending_messages,
    is_wechat_claw_session_timeout,
    queue_wechat_claw_messages,
    record_wechat_claw_interaction,
    remove_wechat_claw_pending_message,
    update_wechat_claw_ilink_state,
    update_wechat_claw_pending_message,
)
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


def test_ilink_message_protocol_parses_context_and_replies_with_msg_envelope():
    adapter = WechatClawAdapter(
        {
            "mode": "ilink",
            "base_url": "https://ilinkai.weixin.qq.com",
            "bot_token": "ilinkbot-test",
            "account_id": "bot@im.bot",
        }
    )
    requests: list[dict] = []

    def fake_request(method, url, body=None, **_kwargs):
        requests.append({"method": method, "url": url, "body": body})
        if url.endswith("/getupdates"):
            return {
                "ret": 0,
                "get_updates_buf": "next-cursor",
                "msgs": [
                    {
                        "message_id": "m-1",
                        "from_user_id": "user@im.wechat",
                        "message_type": 1,
                        "context_token": "ctx-1",
                        "item_list": [{"type": 1, "text_item": {"text": "查询 qB1 状态"}}],
                    }
                ],
            }
        return {}

    adapter._json_request = fake_request  # type: ignore[method-assign]
    updates = adapter.poll_updates(timeout_seconds=1)

    assert updates["raw_count"] == 1
    assert updates["parsed_count"] == 1
    assert updates["messages"] == [
        {
            "user_id": "user@im.wechat",
            "username": "user@im.wechat",
            "message_id": "m-1",
            "text": "查询 qB1 状态",
            "context_token": "ctx-1",
            "raw": updates["messages"][0]["raw"],
        }
    ]

    delivery = adapter.send_text("user@im.wechat", "qB1 当前空闲", "ctx-1")

    assert delivery["sent"] is True
    body = requests[-1]["body"]
    assert body["msg"]["from_user_id"] == "bot@im.bot"
    assert body["msg"]["to_user_id"] == "user@im.wechat"
    assert body["msg"]["context_token"] == "ctx-1"
    assert body["msg"]["item_list"] == [{"type": 1, "text_item": {"text": "qB1 当前空闲"}}]


def test_session_timeout_clears_old_token_but_keeps_waiting_qrcode():
    with SessionLocal() as db:
        update_wechat_claw_ilink_state(
            db,
            bot_token="expired-token",
            account_id="bot@im.bot",
            sync_buf="cursor",
            known_targets={"user@im.wechat": {"context_token": "ctx"}},
            qrcode={"qrcode": "new-login", "status": "waiting"},
        )
        clear_wechat_claw_session(db)
        state = get_wechat_claw_ilink_state(db)

    assert state["bot_token"] == ""
    assert state["account_id"] == ""
    assert state["sync_buf"] == ""
    assert state["known_targets"] == {}
    assert state["pending_messages"] == []
    assert state["qrcode"] == {"qrcode": "new-login", "status": "waiting"}
    assert is_wechat_claw_session_timeout("session timeout") is True
    assert is_wechat_claw_session_timeout("ret=-14") is True


def test_ilink_pending_messages_are_deduplicated_and_keep_reply_until_delivery():
    update = {
        "message_id": "m-queue-1",
        "user_id": "user@im.wechat",
        "text": "查询 qB1 状态",
        "context_token": "ctx-queue-1",
    }
    with SessionLocal() as db:
        update_wechat_claw_ilink_state(db, bot_token="token", sync_buf="old", pending_messages=[])
        assert queue_wechat_claw_messages(db, [update], "new-cursor") == 1
        assert queue_wechat_claw_messages(db, [update], "new-cursor") == 0
        state = get_wechat_claw_ilink_state(db)
        pending = get_wechat_claw_pending_messages(state)
        assert state["sync_buf"] == "new-cursor"
        assert len(pending) == 1
        pending_id = pending[0]["id"]
        update_wechat_claw_pending_message(db, pending_id, reply="qB1 当前空闲")
        assert get_wechat_claw_pending_messages(get_wechat_claw_ilink_state(db))[0]["reply"] == "qB1 当前空闲"
        remove_wechat_claw_pending_message(db, pending_id)
        assert get_wechat_claw_pending_messages(get_wechat_claw_ilink_state(db)) == []
