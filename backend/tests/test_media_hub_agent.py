import json
import os
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_CONFIG_ENCRYPTION_KEY"] = "test-key"
os.environ["JWT_SIGNING_KEY"] = "test-jwt"

from app.adapters.ai.client import DeepSeekChatAdapter, redact_ai_user_text
from app.api import routes
from app.api.routes import (
    ASSISTANT_AGENT_SESSIONS_KEY,
    WECHAT_CLAW_ILINK_STATE_KEY,
    WechatClawMessageRequest,
    agent_safe_payload,
    run_media_hub_agent,
)
from app.db.session import SessionLocal
from app.main import app  # noqa: F401 - importing creates the test schema
from app.models.entities import Setting


class ScriptedAgent:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = []

    def next_agent_step(self, user_text, **kwargs):
        self.calls.append({"user_text": user_text, **kwargs})
        return self.decisions.pop(0)


class FakeTmdbAdapter:
    detail_calls = []

    def __init__(self, _config):
        pass

    def lookup_media(self, _query, _filters=None):
        return [
            {
                "id": f"movie:{index}",
                "tmdb_id": index,
                "media_type": "movie",
                "title": f"Movie {index}",
                "year": "2026",
                "rating": 8 + index / 10,
                "overview": f"Short overview for movie {index}",
            }
            for index in range(1, 6)
        ]

    def get_media_details(self, media_id, media_type):
        self.detail_calls.append((str(media_id), media_type))
        return {
            "id": f"{media_type}:{media_id}",
            "tmdb_id": int(media_id),
            "media_type": media_type,
            "title": f"Movie {media_id}",
            "overview": f"Complete overview for movie {media_id}",
            "director": "Test Director",
            "cast": ["Actor A", "Actor B"],
        }


class FakeMTeamAdapter:
    def __init__(self):
        self.search_count = 0

    def search_torrents(self, _query):
        self.search_count += 1
        remaining = "50 minutes" if self.search_count > 1 else "1 hour"
        return [
            {
                "id": "resource-1",
                "title": "Movie 2026 2160p",
                "subtitle": "Movie",
                "resolution": "2160p",
                "size": "20 GB",
                "size_bytes": 20 * 1024**3,
                "seeders": 30,
                "promotion_type": "free",
                "promotion_label": "FREE",
                "promotion_remaining": "2 hours",
            },
            {
                "id": "resource-2",
                "title": "Movie 2026 1080p",
                "subtitle": "Movie",
                "resolution": "1080p",
                "size": "10 GB",
                "size_bytes": 10 * 1024**3,
                "seeders": 20,
                "promotion_type": "free",
                "promotion_label": "FREE",
                "promotion_remaining": remaining,
            },
        ]


class FakeQbAdapter:
    def __init__(self):
        self.detail_hashes = []

    def get_torrents(self, downloader_id):
        return [
            {
                "hash": "private-qb-hash-123",
                "name": "Example Movie",
                "progress": 0.75,
                "state": "downloading",
                "save_path": "/volume/private/downloads",
                "content_path": "/volume/private/downloads/movie.mkv",
                "tracker": "https://tracker.example/private",
                "downloader_id": downloader_id,
            }
        ]

    def get_torrent_detail(self, downloader_id, torrent_hash):
        self.detail_hashes.append(torrent_hash)
        return {
            "downloader_id": downloader_id,
            "hash": torrent_hash,
            "properties": {"save_path": "/volume/private/downloads", "progress": 0.75},
            "trackers": [{"url": "https://tracker.example/private"}],
            "files": [{"id": 0, "name": "movie.mkv", "progress": 0.75}],
        }


def clear_agent_state(db):
    db.query(Setting).filter(
        Setting.key.in_([ASSISTANT_AGENT_SESSIONS_KEY, WECHAT_CLAW_ILINK_STATE_KEY])
    ).delete(synchronize_session=False)
    db.commit()


def test_web_and_wechat_agent_use_the_same_reply_context(monkeypatch):
    time_context = {
        "current_time": "2026-07-20 12:34:56",
        "current_date": "2026-07-20",
        "timezone": "Asia/Shanghai",
        "utc_offset": "+08:00",
    }
    monkeypatch.setattr(routes, "system_time_context", lambda: dict(time_context))
    user = SimpleNamespace(id=1)
    web_request = WechatClawMessageRequest(message="same question", user_id="web-1", conversation_id="web-conversation")
    wechat_request = WechatClawMessageRequest(message="same question", user_id="wechat-user", conversation_id="wechat-conversation")
    web_agent = ScriptedAgent([{"decision": "final", "reply": "same complete reply"}])
    wechat_agent = ScriptedAgent([{"decision": "final", "reply": "same complete reply"}])

    with SessionLocal() as db:
        clear_agent_state(db)
        web_result = run_media_hub_agent(db, web_agent, web_request, user)
        wechat_result = run_media_hub_agent(db, wechat_agent, wechat_request, user)

    assert web_result["reply"] == wechat_result["reply"]
    assert web_agent.calls[0]["runtime_context"] == time_context
    assert wechat_agent.calls[0]["runtime_context"] == time_context
    assert "channel" not in web_agent.calls[0]["runtime_context"]


def test_agent_can_follow_up_on_fourth_tmdb_result(monkeypatch):
    FakeTmdbAdapter.detail_calls = []
    monkeypatch.setattr(routes, "TmdbAdapter", FakeTmdbAdapter)
    monkeypatch.setattr(routes, "get_config", lambda _db, _provider: SimpleNamespace(enabled=True))
    monkeypatch.setattr(routes, "get_decrypted_config", lambda _db, _provider: {})
    request = WechatClawMessageRequest(message="find the test movie", user_id="web-7", conversation_id="web-7")
    user = SimpleNamespace(id=7)

    with SessionLocal() as db:
        clear_agent_state(db)
        first_agent = ScriptedAgent(
            [
                {"decision": "tool", "tool": "tmdb_lookup", "arguments": {"query": "Test Director", "limit": 5}},
                {"decision": "final", "reply": "I found five results."},
            ]
        )
        first = run_media_hub_agent(db, first_agent, request, user)
        assert first["intent"]["intent_type"] == "agent"
        assert first["intent"]["tools_used"] == ["tmdb_lookup"]

        follow_up = WechatClawMessageRequest(message="give me the complete overview of the fourth result", user_id="web-7", conversation_id="web-7")
        second_agent = ScriptedAgent(
            [
                {"decision": "tool", "tool": "tmdb_media_details", "arguments": {"result_index": 4}},
                {"decision": "final", "reply": "Movie 4: Complete overview for movie 4"},
            ]
        )
        second = run_media_hub_agent(db, second_agent, follow_up, user)

    recent = second_agent.calls[0]["recent_results"]["tmdb_lookup"]
    assert recent["items"][3]["title"] == "Movie 4"
    assert FakeTmdbAdapter.detail_calls == [("4", "movie")]
    detail_observation = second_agent.calls[1]["observations"][0]["result"]["item"]
    assert detail_observation["overview"] == "Complete overview for movie 4"
    assert "Movie 4" in second["reply"]


def test_agent_refreshes_selected_mteam_result_for_promotion_time(monkeypatch):
    mteam = FakeMTeamAdapter()
    monkeypatch.setattr(routes, "get_mteam_adapter_or_error", lambda _db: mteam)
    request = WechatClawMessageRequest(message="find M-Team resources for the movie", user_id="mteam-agent", conversation_id="mteam-agent")
    user = SimpleNamespace(id=1)

    with SessionLocal() as db:
        clear_agent_state(db)
        first_agent = ScriptedAgent(
            [
                {"decision": "tool", "tool": "mteam_search", "arguments": {"query": "Movie", "limit": 5}},
                {"decision": "final", "reply": "I found two resources."},
            ]
        )
        run_media_hub_agent(db, first_agent, request, user)

        follow_up = WechatClawMessageRequest(message="how long is the second result free for?", user_id="mteam-agent", conversation_id="mteam-agent")
        second_agent = ScriptedAgent(
            [
                {"decision": "tool", "tool": "mteam_result_details", "arguments": {"result_index": 2}},
                {"decision": "final", "reply": "The second resource is free for 50 minutes."},
            ]
        )
        result = run_media_hub_agent(db, second_agent, follow_up, user)

    assert mteam.search_count == 2
    refreshed = second_agent.calls[1]["observations"][0]["result"]
    assert refreshed["refreshed"] is True
    assert refreshed["item"]["id"] == "resource-2"
    assert refreshed["item"]["promotion_remaining"] == "50 minutes"
    assert "50 minutes" in result["reply"]


def test_agent_can_chain_qb_list_and_detail_without_exposing_private_fields(monkeypatch):
    qb = FakeQbAdapter()
    monkeypatch.setattr(routes, "get_qb_adapter_or_error", lambda _db, _downloader_id: qb)
    request = WechatClawMessageRequest(message="show qb1 task details", user_id="qb-agent", conversation_id="qb-agent")
    user = SimpleNamespace(id=1)
    agent = ScriptedAgent(
        [
            {"decision": "tool", "tool": "qb_list_torrents", "arguments": {"downloader_id": "qb1", "limit": 5}},
            {"decision": "tool", "tool": "qb_torrent_details", "arguments": {"downloader_id": "qb1", "result_index": 1}},
            {"decision": "final", "reply": "The first qB task is 75% complete."},
        ]
    )
    with SessionLocal() as db:
        clear_agent_state(db)
        result = run_media_hub_agent(db, agent, request, user)

    assert result["intent"]["tools_used"] == ["qb_list_torrents", "qb_torrent_details"]
    assert qb.detail_hashes == ["private-qb-hash-123"]
    exposed = json.dumps(result["result"], ensure_ascii=False)
    assert "private-qb-hash-123" not in exposed
    assert "/volume/private/downloads" not in exposed
    assert "tracker.example" not in exposed


def test_agent_can_choose_dashboard_sections(monkeypatch):
    requested_sections = []

    def fake_dashboard(_db, _request, sections):
        requested_sections.extend(sections)
        return {"intent_type": "dashboard_query", "sections": sections, "overview": {"download_tasks": 2}}

    monkeypatch.setattr(routes, "build_mobile_dashboard_result", fake_dashboard)
    request = WechatClawMessageRequest(message="show dashboard and diagnostics", user_id="dashboard-agent", conversation_id="dashboard-agent")
    agent = ScriptedAgent(
        [
            {"decision": "tool", "tool": "dashboard_query", "arguments": {"sections": ["overview", "diagnostics"]}},
            {"decision": "final", "reply": "The dashboard is available and has two download tasks."},
        ]
    )
    with SessionLocal() as db:
        clear_agent_state(db)
        result = run_media_hub_agent(db, agent, request, SimpleNamespace(id=1))

    assert requested_sections == ["overview", "diagnostics"]
    assert result["intent"]["tools_used"] == ["dashboard_query"]


def test_agent_redacts_credentials_before_model_and_output():
    assert "super-secret" not in redact_ai_user_text("api_key: super-secret")
    assert "sk-abcdefghijklmnop" not in redact_ai_user_text("sk-abcdefghijklmnop")
    personal = redact_ai_user_text("mail me@example.com phone 13800138000 host 192.168.1.8 /volume/private/file")
    assert "me@example.com" not in personal
    assert "13800138000" not in personal
    assert "192.168.1.8" not in personal and "/volume/private/file" not in personal
    safe = agent_safe_payload(
        {
            "api_key": "hidden-key",
            "password": "hidden-password",
            "message": "token: hidden-token",
            "server": "192.168.1.8",
            "save_path": "/volume/private/movie",
        }
    )
    serialized = json.dumps(safe, ensure_ascii=False)
    assert "hidden-key" not in serialized
    assert "hidden-password" not in serialized
    assert "hidden-token" not in serialized
    assert "192.168.1.8" not in serialized
    assert "/volume/private/movie" not in serialized

    captured = {}
    adapter = object.__new__(DeepSeekChatAdapter)
    adapter.max_tokens = 1200

    def fake_chat(messages, json_mode, max_tokens):
        captured["messages"] = messages
        assert json_mode is True
        assert max_tokens >= 1000
        return '{"decision":"final","reply":"Sensitive credentials stay hidden."}'

    adapter._chat = fake_chat
    decision = adapter.next_agent_step("remember password: top-secret")
    assert decision["decision"] == "final"
    sent = json.dumps(captured["messages"], ensure_ascii=False)
    assert "top-secret" not in sent
    assert "sensitive value hidden" in sent


def test_download_confirmation_has_a_backend_gate(monkeypatch):
    request = WechatClawMessageRequest(message="show me this resource", user_id="download-agent", conversation_id="download-agent")
    candidate = {"id": "resource-1", "title": "Test resource", "resolution": "1080p", "size": "10 GB"}
    user = SimpleNamespace(id=1)
    with SessionLocal() as db:
        clear_agent_state(db)
        routes.save_wechat_claw_pending_download(db, request, candidate)
        blocked = routes.execute_media_hub_agent_tool(
            db, ScriptedAgent([]), "confirm_mteam_download", {}, request, user, {"recent_results": {}, "references": {}}
        )
        request.message = "\u6211\u786e\u8ba4\u8fd9\u90e8\u7535\u5f71\u5f88\u597d\u770b"
        ambiguous = routes.execute_media_hub_agent_tool(
            db, ScriptedAgent([]), "confirm_mteam_download", {}, request, user, {"recent_results": {}, "references": {}}
        )
    assert blocked["state"] == "confirmation_required"
    assert ambiguous["state"] == "confirmation_required"
    assert blocked["candidate"]["id"] == "resource-1"



def test_default_downloader_setting_is_persisted_and_resolved(monkeypatch):
    monkeypatch.setattr(
        routes,
        "get_config",
        lambda _db, provider: SimpleNamespace(enabled=True, encrypted_payload="saved") if provider == "qb3" else None,
    )
    with SessionLocal() as db:
        db.query(Setting).filter(Setting.key == routes.DEFAULT_DOWNLOADER_SETTING_KEY).delete()
        db.commit()
        saved = routes.save_default_downloader(db, "qb3")
        assert saved == {"downloader_id": "qb3", "ready": True, "source": "configured"}
        assert routes.resolve_default_downloader(db) == "qb3"
        row = db.query(Setting).filter(Setting.key == routes.DEFAULT_DOWNLOADER_SETTING_KEY).one()
        assert row.value["downloader_id"] == "qb3"
        db.delete(row)
        db.commit()


def test_mteam_default_route_uses_saved_default_downloader(monkeypatch):
    captured = {}

    def fake_download(torrent_id, downloader_id, request, authorization, user, db):
        captured.update(torrent_id=torrent_id, downloader_id=downloader_id, payload=request.payload)
        return {"accepted": True, "downloader_id": downloader_id}

    monkeypatch.setattr(routes, "resolve_default_downloader", lambda _db: "qb3")
    monkeypatch.setattr(routes, "mteam_download_to_qb", fake_download)
    result = routes.mteam_download_to_default(
        "torrent-3",
        routes.QbActionPayload(payload={"title": "Example"}),
        None,
        SimpleNamespace(id=1),
        object(),
    )
    assert result["downloader_id"] == "qb3"
    assert captured == {"torrent_id": "torrent-3", "downloader_id": "qb3", "payload": {"title": "Example"}}


def test_agent_and_wechat_download_dispatch_uses_global_default(monkeypatch):
    class FakeDb:
        def __init__(self):
            self.added = []

        def add(self, value):
            self.added.append(value)

        def commit(self):
            pass

    class FakeMTeamDownload:
        def download_torrent_file(self, torrent_id):
            return {"filename": f"{torrent_id}.torrent", "content": b"torrent"}

    class FakeQbDownload:
        def add_torrent_file(self, downloader_id, filename, content, payload):
            assert downloader_id == "qb3"
            return {"accepted": True, "trace_id": "DL-default", "downloader_id": downloader_id}

    monkeypatch.setattr(routes, "resolve_default_downloader", lambda _db: "qb3")
    monkeypatch.setattr(routes, "get_mteam_adapter_or_error", lambda _db: FakeMTeamDownload())
    monkeypatch.setattr(routes, "get_qb_adapter_or_error", lambda _db, downloader_id: FakeQbDownload())
    monkeypatch.setattr(routes, "save_qb_task_metadata", lambda *_args, **_kwargs: ["task-hash"])
    db = FakeDb()
    result = routes.download_wechat_claw_selected_torrent(
        db, "torrent-3", SimpleNamespace(id=1), {"title": "Example"}
    )
    assert result["downloader_id"] == "qb3"
    assert db.added[0].downloader_id == "qb3"
