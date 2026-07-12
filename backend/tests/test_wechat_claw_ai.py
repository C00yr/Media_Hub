import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_CONFIG_ENCRYPTION_KEY"] = "test-key"
os.environ["JWT_SIGNING_KEY"] = "test-jwt"

from fastapi.testclient import TestClient

from app.adapters.ai.client import normalize_assistant_intent, render_mobile_reply
from app.adapters.wechat_claw import WechatClawAdapter
from app.api.routes import (
    WechatClawMessageRequest,
    clear_wechat_claw_session,
    get_wechat_claw_mobile_search_result,
    get_wechat_claw_ilink_state,
    get_wechat_claw_pending_messages,
    is_wechat_claw_session_timeout,
    queue_wechat_claw_messages,
    format_mobile_agent_reply,
    grant_wechat_claw_privacy_access,
    has_wechat_claw_privacy_access,
    rank_mteam_search_items,
    record_wechat_claw_interaction,
    remove_wechat_claw_pending_message,
    save_wechat_claw_mobile_candidates,
    mteam_model_rows,
    tmdb_filters_for_request,
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
    assert "dashboard_query" in {item["action"] for item in setup_body["capabilities"]}
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


def test_wechat_claw_bindings_keep_identity_preferences_and_sessions_isolated():
    token = admin_token()
    headers = auth_headers(token)
    first = client.post(
        "/api/admin/wechat-claw/bindings",
        headers=headers,
        json={"display_name": "客厅微信", "role_name": "客厅管家", "enabled": True},
    )
    second = client.post(
        "/api/admin/wechat-claw/bindings",
        headers=headers,
        json={"display_name": "卧室微信", "role_name": "卧室管家", "enabled": True},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    first_id = first.json()["id"]
    second_id = second.json()["id"]

    updated = client.patch(
        f"/api/admin/wechat-claw/bindings/{first_id}",
        headers=headers,
        json={
            "display_name": "客厅微信",
            "role_name": "电影推荐官",
            "enabled": True,
            "notification_preferences": {"download_started": False, "download_completed": True},
        },
    )
    assert updated.status_code == 200
    assert updated.json()["role_name"] == "电影推荐官"
    assert updated.json()["notification_preferences"]["download_started"] is False

    with SessionLocal() as db:
        update_wechat_claw_ilink_state(db, first_id, bot_token="first-token", sync_buf="first-cursor")
        update_wechat_claw_ilink_state(db, second_id, bot_token="second-token", sync_buf="second-cursor")
        assert get_wechat_claw_ilink_state(db, first_id)["bot_token"] == "first-token"
        assert get_wechat_claw_ilink_state(db, second_id)["bot_token"] == "second-token"

    first_setup = client.get(f"/api/admin/wechat-claw/bindings/{first_id}/setup?refresh_remote=false", headers=headers)
    second_setup = client.get(f"/api/admin/wechat-claw/bindings/{second_id}/setup?refresh_remote=false", headers=headers)
    assert first_setup.status_code == 200
    assert second_setup.status_code == 200
    assert first_setup.json()["binding"]["role_name"] == "电影推荐官"
    assert first_setup.json()["account_id"] != second_setup.json()["account_id"] or first_setup.json()["binding"]["id"] != second_setup.json()["binding"]["id"]


def test_mobile_agent_intents_ranking_and_templates_are_stable():
    intent = normalize_assistant_intent(
        {
            "intent_type": "tmdb_lookup",
            "query": "",
            "tmdb_filters": {"media_type": "tv", "region": "KR", "min_rating": 8, "sort_by": "vote_average.desc"},
        }
    )
    assert intent["intent_type"] == "tmdb_lookup"
    assert intent["tmdb_filters"]["media_type"] == "tv"
    assert intent["tmdb_filters"]["min_rating"] == 8

    tmdb_error_reply = format_mobile_agent_reply(
        {"intent_type": "tmdb_lookup"},
        {"intent_type": "tmdb_lookup", "state": "failed", "error": "TMDB 查询失败：连接超时"},
    )
    assert "TMDB 查询失败" in tmdb_error_reply
    assert "没有找到符合条件" not in tmdb_error_reply

    anime_filters = tmdb_filters_for_request(
        {"query": "尼古喵喵", "tmdb_filters": {"media_type": "movie"}},
        "搜一下尼古喵喵这部动画",
    )
    assert anime_filters["media_type"] == "tv"
    title_filters = tmdb_filters_for_request(
        {"query": "尼古喵喵", "tmdb_filters": {"media_type": "movie"}},
        "查一下尼古喵喵",
    )
    assert title_filters["media_type"] == "all"

    ranked, recommended_index = rank_mteam_search_items(
        [
            {"id": "normal", "resolution": "2160p", "size_bytes": 35 * 1024**3, "seeders": 90, "promotion_type": ""},
            {"id": "free", "resolution": "2160p", "size_bytes": 35 * 1024**3, "seeders": 30, "promotion_type": "free", "promotion_label": "FREE"},
        ],
        {"recommend": True},
    )
    assert ranked[0]["id"] == "free"
    assert recommended_index == 1
    reply = format_mobile_agent_reply({"intent_type": "mteam_search"}, {"items": ranked, "count": 2, "recommended_index": 1})
    assert "| # | 标题 | 中文信息 | 清晰度 | 大小 | 做种 | 促销 |" in reply
    assert "推荐第 1 个" in reply


def test_mteam_reply_is_compact_and_can_expand_cached_top_ten():
    request = WechatClawMessageRequest(message="搜索痴迷资源", user_id="mteam-user", conversation_id="mteam-chat")
    items = [
        {
            "id": f"resource-{index}",
            "title": f"Obsession 2025 2160p WEB-DL H.265-Group{index}",
            "subtitle": "痴迷 / Obsession / 爱你致死不渝 | 2025 | 美国 | 恐怖 惊悚 | 演员甲 / 演员乙",
            "resolution": "2160p",
            "codec": "H.265",
            "hdr": "DV HDR",
            "group": f"Group{index}",
            "size": f"{10 + index}.00 GB",
            "size_bytes": (10 + index) * 1024**3,
            "seeders": 20 - index,
            "promotion_label": "50%",
        }
        for index in range(1, 11)
    ]
    presentation = mteam_model_rows(items)
    initial_reply = format_mobile_agent_reply(
        {"intent_type": "mteam_search"},
        {"items": items, "count": 10, "recommended_index": 1, "presentation": presentation},
    )
    assert "10 个资源" in initial_reply
    assert "前 **5** 个" in initial_reply
    assert "演员甲" not in initial_reply
    assert "2025 · 美国 · 恐怖/惊悚 · 未标注中字" in initial_reply
    assert initial_reply.count("| 1 |") == 1
    assert "| 6 |" not in initial_reply

    with SessionLocal() as db:
        save_wechat_claw_mobile_candidates(db, request, items, query="痴迷", recommended_index=1, presentation=presentation)
        expanded = get_wechat_claw_mobile_search_result(db, request)
        assert expanded is not None
        assert len(expanded["items"]) == 10
        assert expanded["items"][9]["id"] == "resource-10"
        expanded_reply = format_mobile_agent_reply({"intent_type": "mteam_search"}, expanded)
        assert "完整列表" in expanded_reply
        assert "| 10 |" in expanded_reply


def test_mobile_privacy_grant_is_scoped_to_wechat_user():
    request = WechatClawMessageRequest(message="验证管理员密码：secret", user_id="private-user", conversation_id="private-conversation")
    other_request = WechatClawMessageRequest(message="qB2 详情", user_id="other-user", conversation_id="other-conversation")
    with SessionLocal() as db:
        grant_wechat_claw_privacy_access(db, request)
        assert has_wechat_claw_privacy_access(db, request) is True
        assert has_wechat_claw_privacy_access(db, other_request) is False
