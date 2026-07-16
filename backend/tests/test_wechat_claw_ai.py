import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_CONFIG_ENCRYPTION_KEY"] = "test-key"
os.environ["JWT_SIGNING_KEY"] = "test-jwt"

from fastapi.testclient import TestClient

from app.adapters.ai.client import normalize_assistant_intent, render_mobile_reply
from app.adapters.wechat_claw import WechatClawAdapter
from app.api.routes import (
    WECHAT_CLAW_INTERACTIONS_KEY,
    WechatClawMessageRequest,
    _WECHAT_CLAW_ACTIVE_USER_ID,
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
from app.models.entities import Setting


client = TestClient(app)


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def admin_token() -> str:
    for password in ("password123", "adminadmin"):
        login = client.post("/api/auth/login", json={"username": "admin", "password": password})
        if login.status_code == 200:
            return login.json()["access_token"]
    raise AssertionError("default administrator login failed")


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

    health = client.get("/api/diagnostics/health", headers=auth_headers(token))
    assert health.status_code == 200
    modules = {item["module"]: item for item in health.json()["modules"]}
    assert modules["wechat_claw"]["status"] == "failed"
    assert modules["wechat_claw"]["last_error"] == "尚未完成微信扫码绑定。"
    assert "stats_engine" not in modules

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

    monitored = client.post(
        "/api/assistant/execute",
        headers=auth_headers(token),
        json={"message": "测试完成通知", "intent": {"action": "download_completed", "message": "测试完成通知"}},
    )
    assert monitored.status_code == 200
    assert monitored.json()["result"]["monitoring"] is True

def test_disabled_wechat_claw_session_is_not_reported_as_operational():
    token = admin_token()
    headers = auth_headers(token)
    tested = client.post(
        "/api/admin/integrations/wechat_claw/test",
        headers=headers,
        json={"payload": {"mode": "ilink", "poll_timeout": 25, "default_downloader_id": "all"}},
    )
    assert tested.status_code == 200
    assert client.post("/api/admin/integrations/wechat_claw/enable", headers=headers).status_code == 200

    listed = client.get("/api/admin/wechat-claw/bindings", headers=headers)
    assert listed.status_code == 200
    member = listed.json()["items"][0]
    binding_id = member["id"]

    enabled_member = client.patch(
        f"/api/admin/wechat-claw/bindings/{binding_id}",
        headers=headers,
        json={
            "display_name": member["display_name"],
            "role_name": member["role_name"],
            "enabled": True,
            "notification_preferences": member["notification_preferences"],
        },
    )
    assert enabled_member.status_code == 200
    with SessionLocal() as db:
        update_wechat_claw_ilink_state(db, binding_id, bot_token="preserved-token", account_id="wx-preserved")

    health = client.get("/api/diagnostics/health", headers=headers)
    active_member = next(item for item in health.json()["wechat_members"] if item["id"] == binding_id)
    assert active_member["connected"] is True
    assert active_member["module_enabled"] is True
    assert active_member["operational"] is True

    assert client.post("/api/admin/integrations/wechat_claw/disable", headers=headers).status_code == 200
    health = client.get("/api/diagnostics/health", headers=headers)
    disabled_module_member = next(item for item in health.json()["wechat_members"] if item["id"] == binding_id)
    assert disabled_module_member["connected"] is True
    assert disabled_module_member["module_enabled"] is False
    assert disabled_module_member["operational"] is False

    assert client.post("/api/admin/integrations/wechat_claw/enable", headers=headers).status_code == 200
    disabled_member = client.patch(
        f"/api/admin/wechat-claw/bindings/{binding_id}",
        headers=headers,
        json={
            "display_name": member["display_name"],
            "role_name": member["role_name"],
            "enabled": False,
            "notification_preferences": member["notification_preferences"],
        },
    )
    assert disabled_member.status_code == 200
    health = client.get("/api/diagnostics/health", headers=headers)
    inactive_member = next(item for item in health.json()["wechat_members"] if item["id"] == binding_id)
    assert inactive_member["connected"] is True
    assert inactive_member["enabled"] is False
    assert inactive_member["operational"] is False

    restore_member = client.patch(
        f"/api/admin/wechat-claw/bindings/{binding_id}",
        headers=headers,
        json={
            "display_name": member["display_name"],
            "role_name": member["role_name"],
            "enabled": True,
            "notification_preferences": member["notification_preferences"],
        },
    )
    assert restore_member.status_code == 200

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
            "notification_preferences": {"qb1_download_started": False, "qb1_download_completed": True},
        },
    )
    assert updated.status_code == 200
    assert updated.json()["role_name"] == "电影推荐官"
    assert updated.json()["notification_preferences"]["qb1_download_started"] is False

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


def test_wechat_claw_binding_member_limit_is_enforced():
    token = admin_token()
    headers = auth_headers(token)
    listed = client.get("/api/admin/wechat-claw/bindings", headers=headers)
    assert listed.status_code == 200
    limit = listed.json()["limit"]
    initial_count = len(listed.json()["items"])
    created_ids: list[int] = []

    try:
        for index in range(initial_count, limit):
            created = client.post(
                "/api/admin/wechat-claw/bindings",
                headers=headers,
                json={"display_name": f"limit-member-{index + 1}"},
            )
            assert created.status_code == 200
            created_ids.append(created.json()["id"])

        blocked = client.post(
            "/api/admin/wechat-claw/bindings",
            headers=headers,
            json={"display_name": "member-over-limit"},
        )
        assert blocked.status_code == 409
        assert "最多可添加 5 位微信成员" in blocked.json()["detail"]

        after = client.get("/api/admin/wechat-claw/bindings", headers=headers)
        assert len(after.json()["items"]) == max(initial_count, limit)
    finally:
        for binding_id in created_ids:
            deleted = client.delete(f"/api/admin/wechat-claw/bindings/{binding_id}", headers=headers)
            assert deleted.status_code == 200


def test_wechat_claw_binding_interactions_do_not_collide_with_legacy_global_key():
    token = admin_token()
    headers = auth_headers(token)
    created = client.post(
        "/api/admin/wechat-claw/bindings",
        headers=headers,
        json={"display_name": "phone-wechat", "role_name": "family-helper", "enabled": True},
    )
    assert created.status_code == 200
    binding_id = created.json()["id"]

    with SessionLocal() as db:
        legacy = db.query(Setting).filter(Setting.key == WECHAT_CLAW_INTERACTIONS_KEY).one_or_none()
        if legacy is None:
            db.add(Setting(key=WECHAT_CLAW_INTERACTIONS_KEY, value={"items": []}))
            db.commit()
        scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding_id)
        try:
            item = record_wechat_claw_interaction(
                db,
                WechatClawMessageRequest(message="dashboard status", user_id="wx-bound-user", conversation_id="conv-bound"),
                {"action": "status_query", "intent_type": "dashboard_query"},
                {"target": "dashboard"},
                "dashboard ok",
            )
        finally:
            _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)

        scoped = db.query(Setting).filter(Setting.key == f"{WECHAT_CLAW_INTERACTIONS_KEY}.binding.{binding_id}").one_or_none()
        assert item["user_id"] == "wx-bound-user"
        assert scoped is not None
        assert scoped.value["items"][0]["user_id"] == "wx-bound-user"
        assert db.query(Setting).filter(Setting.key == WECHAT_CLAW_INTERACTIONS_KEY).count() == 1


def test_tmdb_title_lookup_discards_ai_guessed_language_and_region():
    localized_title_filters = tmdb_filters_for_request(
        {
            "query": "\u975e\u81ea\u7136\u6b7b\u4ea1",
            "tmdb_filters": {"media_type": "tv", "region": "CN", "language": "zh"},
        },
        "\u5e2e\u6211\u641c\u4e00\u4e0b\u975e\u81ea\u7136\u6b7b\u4ea1",
    )
    assert localized_title_filters["media_type"] == "all"
    assert localized_title_filters["region"] == ""
    assert localized_title_filters["language"] == ""

    discovery_filters = tmdb_filters_for_request(
        {
            "query": "",
            "tmdb_filters": {"media_type": "tv", "region": "KR", "language": "ko"},
        },
        "\u63a8\u8350\u51e0\u90e8\u97e9\u56fd\u7535\u89c6\u5267",
    )
    assert discovery_filters["region"] == "KR"
    assert discovery_filters["language"] == "ko"


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
    assert "【TMDB" not in tmdb_error_reply

    tmdb_reply = format_mobile_agent_reply(
        {"intent_type": "tmdb_lookup"},
        {
            "items": [
                {
                    "title": "星际穿越",
                    "original_title": "Interstellar",
                    "media_type": "movie",
                    "genres": ["科幻", "剧情"],
                    "year": "2014",
                    "rating": 8.7,
                    "production_countries": ["美国", "英国"],
                    "runtime": 169,
                    "director": "克里斯托弗·诺兰",
                    "cast": ["马修·麦康纳", "安妮·海瑟薇"],
                    "overview": "一支探险队穿越虫洞，为人类寻找新的家园。",
                },
                {
                    "title": "最后生还者",
                    "media_type": "tv",
                    "genres": ["剧情", "科幻"],
                    "year": "2023",
                    "rating": 8.6,
                    "production_countries": ["美国"],
                    "runtime": 50,
                    "number_of_seasons": 3,
                    "number_of_episodes": 16,
                    "last_episode_to_air": {"season_number": 3, "episode_number": 8},
                    "director": "克雷格·麦辛",
                    "cast": ["佩德罗·帕斯卡", "贝拉·拉姆齐"],
                    "overview": "末日后的护送旅程。",
                },
            ]
        },
    )
    assert "【TMDB 影视查询】" not in tmdb_reply
    assert "| # |" not in tmdb_reply
    assert "- **1. 星际穿越 / Interstellar**" in tmdb_reply
    assert "- 分类：电影 · 类型：科幻 / 剧情" in tmdb_reply
    assert "克里斯托弗·诺兰" in tmdb_reply
    assert "每集 50 分钟 · 共 3 季 · 16 集 · 更新至 S3E8" in tmdb_reply
    assert "推荐" not in tmdb_reply

    dashboard_reply = format_mobile_agent_reply(
        {"intent_type": "dashboard_query"},
        {
            "overview": {
                "total_upload_speed": 5 * 1024**2,
                "download_tasks": 2,
                "upload_tasks": 15,
                "nas_space_label": "6.20 TiB / 10.00 TiB",
                "nas_usage_percent": 62,
                "nas_storage_readable": True,
            },
            "mteam": {
                "user_level": "Power User",
                "ratio": 3.42,
                "bonus": 128560,
                "bonus_delta_label": "+860.0",
                "delta_window_label": "近5h",
                "traffic_series": {
                    "hour": [{"upload_total": 2 * 1024**3}],
                    "day": [{"upload_total": 38 * 1024**3}],
                    "week": [{"upload_total": 286 * 1024**3}],
                    "month": [{"upload_total": 912 * 1024**3}],
                },
            },
            "qbs": [{"id": "qb1", "name": "主下载器", "enabled": True, "online": True, "errors": 0}],
        },
    )
    assert "仪表盘状态：运行正常" in dashboard_reply
    assert "核心指标" in dashboard_reply
    assert "今日上传：38.00 GB · 近一小时：2.00 GB" in dashboard_reply
    assert "本周上传：286.00 GB · 本月上传：912.00 GB" in dashboard_reply
    assert "分享率：3.42 · 魔力值：128560（近5h +860.0）" in dashboard_reply
    assert "运行概览" in dashboard_reply
    assert "需要关注" not in dashboard_reply

    dashboard_alert_reply = format_mobile_agent_reply(
        {"intent_type": "dashboard_query"},
        {
            "overview": {"nas_storage_readable": False},
            "mteam": {"user_level": "-"},
            "qbs": [{"id": "qb1", "enabled": True, "online": False}],
        },
    )
    assert "仪表盘状态：需要关注" in dashboard_alert_reply
    assert "- NAS 存储不可访问" in dashboard_alert_reply
    assert "- M-Team 数据不可用" in dashboard_alert_reply
    assert "- qB1 离线" in dashboard_alert_reply

    mteam_reply = format_mobile_agent_reply(
        {"intent_type": "dashboard_query"},
        {
            "mteam": {
                "username": "media_user",
                "user_level": "Power User",
                "joined_at": "2024-01-01",
                "vip": True,
                "allow_download": True,
                "warned": False,
                "upload_total": 2 * 1024**4,
                "upload_delta_label": "+5.00 GB",
                "download_total": 512 * 1024**3,
                "download_delta_label": "+1.00 GB",
                "ratio": 3.42,
                "ratio_delta_label": "+0.008",
                "bonus": 128560,
                "bonus_delta_label": "+860.0",
                "seed_count": 42,
                "seed_size": 3 * 1024**4,
                "seed_size_delta_label": "+12.00 GB",
                "active_uploads": 8,
                "active_downloads": 1,
                "active_delta_label": "上传 +2 / 下载 -1",
                "seedtime_seconds": 5 * 86400 + 3 * 3600,
                "leechtime_seconds": 2 * 3600 + 5 * 60,
                "delta_window_label": "近5h",
                "updated_at": "2026-07-13T10:30:00",
                "source": "M-Team 原始数据（Real API）",
                "traffic_series": {
                    dimension: [{"upload_total": upload, "download_total": download}]
                    for dimension, upload, download in [
                        ("hour", 2 * 1024**3, 1 * 1024**3),
                        ("day", 38 * 1024**3, 4 * 1024**3),
                        ("week", 286 * 1024**3, 25 * 1024**3),
                        ("month", 912 * 1024**3, 96 * 1024**3),
                    ]
                },
            }
        },
    )
    assert "M-Team 站点数据" in mteam_reply
    assert "账号状态" in mteam_reply
    assert "流量与收益" in mteam_reply
    assert "累计上传：2.00 TB（近5h +5.00 GB）" in mteam_reply
    assert "分享率：3.420（近5h +0.008）" in mteam_reply
    assert "做种与活动" in mteam_reply
    assert "当前活跃：上传 8 · 下载 1（近5h 上传 +2 / 下载 -1）" in mteam_reply
    assert "本地快照流量" in mteam_reply
    assert "今日：上传 38.00 GB · 下载 4.00 GB" in mteam_reply

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
