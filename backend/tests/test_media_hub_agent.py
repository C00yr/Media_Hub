import json
import os
from types import SimpleNamespace
from urllib.error import URLError

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


class FuzzyTmdbAdapter:
    detail_calls = []

    def __init__(self, _config):
        pass

    def search_people(self, query, limit=5):
        mapping = {"梁朝伟": 1, "汤唯": 2}
        return [{"person_id": mapping[query], "name": query}] if query in mapping else []

    def get_person_details(self, person_id):
        return {
            "person_id": int(person_id),
            "name": "梁朝伟" if str(person_id) == "1" else "汤唯",
            "credits": [
                {
                    "id": "movie-4347",
                    "tmdb_id": 4347,
                    "media_type": "movie",
                    "title": "色，戒",
                    "year": "2007",
                    "popularity": 30,
                }
            ],
        }

    def get_media_details(self, media_id, media_type):
        self.detail_calls.append((str(media_id), media_type))
        return {
            "id": f"{media_type}-{media_id}",
            "tmdb_id": int(media_id),
            "media_type": media_type,
            "title": "色，戒",
            "year": "2007",
            "director": "李安",
            "cast": ["梁朝伟", "汤唯"],
            "genres": ["剧情", "爱情"],
            "production_countries": ["中国", "美国"],
            "rating": 7.3,
            "vote_count": 8200,
            "overview": "抗战时期，一名女子被安排接近情报头目。",
            "popularity": 30,
        }

    def search_media(self, query):
        return [self.get_media_details("4347", "movie")] if query in {"色，戒", "色戒"} else []


class MemoryAgent(ScriptedAgent):
    def __init__(self, decisions, candidate):
        super().__init__(decisions)
        self.candidate = candidate

    def guess_media_from_memory(self, _user_text, history=None):
        return dict(self.candidate)


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


def test_web_agent_qb2_access_uses_explicit_browser_grant(monkeypatch):
    qb = FakeQbAdapter()
    monkeypatch.setattr(routes, "get_qb_adapter_or_error", lambda _db, _downloader_id: qb)
    user = SimpleNamespace(id=1)

    denied_request = WechatClawMessageRequest(
        message="show qb2 tasks", user_id="web-1", conversation_id="web-qb2-denied"
    )
    denied_agent = ScriptedAgent(
        [
            {"decision": "tool", "tool": "qb_list_torrents", "arguments": {"downloader_id": "qb2"}},
            {"decision": "final", "reply": "Administrator verification is required."},
        ]
    )
    with SessionLocal() as db:
        clear_agent_state(db)
        denied = run_media_hub_agent(
            db, denied_agent, denied_request, user, qb2_authorized=False
        )

    denied_observation = denied["result"]["observations"][0]["result"]
    assert denied_observation["state"] == "privacy_required"

    allowed_request = WechatClawMessageRequest(
        message="show qb2 tasks", user_id="web-1", conversation_id="web-qb2-allowed"
    )
    allowed_agent = ScriptedAgent(
        [
            {"decision": "tool", "tool": "qb_list_torrents", "arguments": {"downloader_id": "qb2"}},
            {"decision": "final", "reply": "One qB2 task is available."},
        ]
    )
    with SessionLocal() as db:
        clear_agent_state(db)
        allowed = run_media_hub_agent(
            db, allowed_agent, allowed_request, user, qb2_authorized=True
        )

    allowed_observation = allowed["result"]["observations"][0]["result"]
    assert allowed_observation["state"] == "success"
    assert allowed_observation["downloader_id"] == "qb2"
    assert allowed_observation["count"] == 1


def test_dashboard_qb2_summary_uses_the_same_privacy_grant(monkeypatch):
    monkeypatch.setattr(
        routes,
        "build_dashboard_payload",
        lambda _db: {
            "overview": {"download_tasks": 2},
            "mteam": {},
            "qbs": [
                {"id": "qb1", "active_downloads": 1, "active_uploads": 0},
                {"id": "qb2", "active_downloads": 0, "active_uploads": 1},
            ],
            "updated_at": "2026-07-22T08:00:00Z",
        },
    )
    request = WechatClawMessageRequest(
        message="show dashboard", user_id="web-1", conversation_id="web-dashboard"
    )

    with SessionLocal() as db:
        denied = routes.build_mobile_dashboard_result(
            db, request, ["overview", "downloads", "qb2"], qb2_authorized=False
        )
        allowed = routes.build_mobile_dashboard_result(
            db, request, ["overview", "downloads", "qb2"], qb2_authorized=True
        )

    assert denied["qb2_privacy_required"] is True
    assert [item["id"] for item in denied["qbs"]] == ["qb1", "qb2"]
    assert [item["id"] for item in denied["downloads"]] == ["qb1", "qb2"]
    assert denied["qb2"]["id"] == "qb2"

    assert allowed.get("qb2_privacy_required") is not True
    assert [item["id"] for item in allowed["qbs"]] == ["qb1", "qb2"]
    assert [item["id"] for item in allowed["downloads"]] == ["qb1", "qb2"]
    assert allowed["qb2"]["id"] == "qb2"


def test_contextual_chinese_ordinal_uses_recent_tmdb_list_without_model_guessing(monkeypatch):
    FakeTmdbAdapter.detail_calls = []
    monkeypatch.setattr(routes, "TmdbAdapter", FakeTmdbAdapter)
    monkeypatch.setattr(routes, "get_config", lambda _db, _provider: SimpleNamespace(enabled=True))
    monkeypatch.setattr(routes, "get_decrypted_config", lambda _db, _provider: {})
    user = SimpleNamespace(id=1)
    with SessionLocal() as db:
        clear_agent_state(db)
        run_media_hub_agent(
            db,
            ScriptedAgent([
                {"decision": "tool", "tool": "tmdb_lookup", "arguments": {"query": "Movie", "limit": 5}},
                {"decision": "final", "reply": "ignored"},
            ]),
            WechatClawMessageRequest(message="帮我搜一下 Movie", user_id="ordinal", conversation_id="ordinal"),
            user,
        )
        should_not_be_called = ScriptedAgent([])
        result = run_media_hub_agent(
            db,
            should_not_be_called,
            WechatClawMessageRequest(message="第二部的演员有哪些", user_id="ordinal", conversation_id="ordinal"),
            user,
        )
    assert should_not_be_called.calls == []
    assert FakeTmdbAdapter.detail_calls == [("2", "movie")]
    assert "Actor A / Actor B" in result["reply"]


def test_fuzzy_identification_and_detail_follow_up_cannot_loop_in_conversation(monkeypatch):
    FuzzyTmdbAdapter.detail_calls = []
    monkeypatch.setattr(routes, "TmdbAdapter", FuzzyTmdbAdapter)
    monkeypatch.setattr(routes, "get_config", lambda _db, _provider: SimpleNamespace(enabled=True))
    monkeypatch.setattr(routes, "get_decrypted_config", lambda _db, _provider: {})
    request = WechatClawMessageRequest(
        message="有一部电影，梁朝伟演的，女主是汤唯，这是哪部电影？",
        user_id="fuzzy-agent",
        conversation_id="fuzzy-agent",
    )
    user = SimpleNamespace(id=1)
    with SessionLocal() as db:
        clear_agent_state(db)
        first = run_media_hub_agent(
            db,
            ScriptedAgent(
                [
                    {"decision": "final", "reply": "我印象里是《色，戒》。需要我查吗？"},
                    {"decision": "final", "reply": "需要我帮你查详情还是资源吗？"},
                ]
            ),
            request,
            user,
        )
        follow_up = run_media_hub_agent(
            db,
            ScriptedAgent(
                [
                    {"decision": "final", "reply": "需要我帮您查一下吗？"},
                    {"decision": "final", "reply": "您确认要查吗？"},
                ]
            ),
            WechatClawMessageRequest(
                message="给我这部电影的详细信息",
                user_id="fuzzy-agent",
                conversation_id="fuzzy-agent",
            ),
            user,
        )
        session = routes.get_assistant_agent_session(db, request)
        trace = db.query(routes.DebugTrace).filter(routes.DebugTrace.event_type == "agent_run").order_by(
            routes.DebugTrace.id.desc()
        ).first()

    assert first["intent"]["tools_used"] == ["resolve_media_from_clues"]
    assert "《色，戒》（2007）" in first["reply"]
    assert follow_up["intent"]["tools_used"] == ["tmdb_media_details"]
    assert "抗战时期" in follow_up["reply"]
    assert session["current_work"]["title"] == "色，戒"
    assert trace.timeline[0]["obligation_violations"] == 2
    assert trace.timeline[0]["deterministic_takeover"] is True


def test_bare_lookup_uses_current_verified_work(monkeypatch):
    FuzzyTmdbAdapter.detail_calls = []
    monkeypatch.setattr(routes, "TmdbAdapter", FuzzyTmdbAdapter)
    monkeypatch.setattr(routes, "get_config", lambda _db, _provider: SimpleNamespace(enabled=True))
    monkeypatch.setattr(routes, "get_decrypted_config", lambda _db, _provider: {})
    user = SimpleNamespace(id=1)
    request = WechatClawMessageRequest(message="查", user_id="current-work", conversation_id="current-work")
    with SessionLocal() as db:
        clear_agent_state(db)
        session = {
            "history": [],
            "recent_results": {},
            "references": {},
            "current_list": {},
            "current_work": {
                "tmdb_id": "4347",
                "media_type": "movie",
                "title": "色，戒",
                "year": "2007",
                "verified_at": routes.utc_iso(),
            },
        }
        routes.save_assistant_agent_session(db, request, session)
        result = run_media_hub_agent(
            db,
            ScriptedAgent(
                [
                    {"decision": "final", "reply": "你想查详情还是资源？"},
                    {"decision": "final", "reply": "请再说清楚一点。"},
                ]
            ),
            request,
            user,
        )
    assert result["intent"]["tools_used"] == ["tmdb_media_details"]
    assert "色，戒" in result["reply"]


def test_subjective_chat_has_no_tool_obligation():
    obligations = routes.determine_agent_tool_obligations(
        "你觉得李安的电影风格怎么样？",
        {"current_work": {}, "pending_request": {}},
    )
    assert obligations == []


def test_live_status_and_resource_requests_have_backend_tool_obligations():
    current = {
        "current_work": {"tmdb_id": "4347", "media_type": "movie", "title": "色，戒", "year": "2007"},
        "pending_request": {},
    }
    assert routes.determine_agent_tool_obligations("馒头站点现在状态怎么样？", current)[0]["tool"] == "dashboard_query"
    assert routes.determine_agent_tool_obligations("查一下这部电影的资源", current)[0]["tool"] == "mteam_search"
    assert routes.determine_agent_tool_obligations("不用查，聊聊这部电影", current) == []


def test_conflicting_fuzzy_clue_is_explained_and_one_confirmation_sets_current_work(monkeypatch):
    monkeypatch.setattr(routes, "TmdbAdapter", FuzzyTmdbAdapter)
    monkeypatch.setattr(routes, "get_config", lambda _db, _provider: SimpleNamespace(enabled=True))
    monkeypatch.setattr(routes, "get_decrypted_config", lambda _db, _provider: {})
    user = SimpleNamespace(id=1)
    request = WechatClawMessageRequest(
        message="梁朝伟和汤唯主演、王家卫导演的是哪部电影？",
        user_id="conflict-agent",
        conversation_id="conflict-agent",
    )
    with SessionLocal() as db:
        clear_agent_state(db)
        result = run_media_hub_agent(
            db,
            ScriptedAgent(
                [
                    {"decision": "final", "reply": "可能是色戒。"},
                    {"decision": "final", "reply": "你要我查吗？"},
                ]
            ),
            request,
            user,
        )
        accepted = run_media_hub_agent(
            db,
            ScriptedAgent([]),
            WechatClawMessageRequest(
                message="是的",
                user_id="conflict-agent",
                conversation_id="conflict-agent",
            ),
            user,
        )
        session = routes.get_assistant_agent_session(db, request)
    assert "导演是 李安" in result["reply"]
    assert "不是 王家卫" in result["reply"]
    assert "已按《色，戒》" in accepted["reply"]
    assert session["current_work"]["title"] == "色，戒"


def test_memory_guess_is_friendly_when_tmdb_network_verification_fails(monkeypatch):
    class OfflineTmdbAdapter:
        def __init__(self, _config):
            pass

        def search_people(self, _query, limit=5):
            raise URLError("network unavailable")

    monkeypatch.setattr(routes, "TmdbAdapter", OfflineTmdbAdapter)
    monkeypatch.setattr(routes, "get_config", lambda _db, _provider: SimpleNamespace(enabled=True))
    monkeypatch.setattr(routes, "get_decrypted_config", lambda _db, _provider: {})
    request = WechatClawMessageRequest(
        message="梁朝伟和汤唯演的是哪部电影？",
        user_id="memory-agent",
        conversation_id="memory-agent",
    )
    agent = MemoryAgent(
        [
            {"decision": "final", "reply": "是色戒。"},
            {"decision": "final", "reply": "需要查吗？"},
        ],
        {"title": "色，戒", "year": "2007", "reason": "演员线索"},
    )
    with SessionLocal() as db:
        clear_agent_state(db)
        result = run_media_hub_agent(db, agent, request, SimpleNamespace(id=1))
        session = routes.get_assistant_agent_session(db, request)
    assert "最可能是《色，戒》（2007）" in result["reply"]
    assert "连接 TMDB 时遇到网络问题" in result["reply"]
    assert "未核验猜测" not in result["reply"]
    assert session["current_work"] == {}


def test_promotion_filters_are_hard_and_include_30_percent_as_better_than_half():
    items = [
        {"id": "normal", "download_factor": 1.0, "seeders": 100},
        {"id": "half", "download_factor": 0.5, "seeders": 10},
        {"id": "thirty", "download_factor": 0.3, "seeders": 8},
        {"id": "free", "download_factor": 0.0, "seeders": 2},
    ]
    ranked, _ = routes.rank_mteam_search_items(items, {"download_factor_max": 0.5})
    assert {item["id"] for item in ranked} == {"half", "thirty", "free"}
    assert ranked[0]["id"] == "free"


def test_composite_template_reports_requested_count_without_padding():
    reply = routes.format_composite_media_reply(
        {
            "target_count": 5,
            "checked_works": 12,
            "complete": True,
            "queried_at": "2026-07-29T12:00:00Z",
            "matches": [
                {
                    "work": {"title": "作品一", "year": "2025", "rating": 8.2, "vote_count": 500},
                    "resource": {"title": "Release", "size": "20 GB", "seeders": 12, "promotion_label": "50%"},
                }
            ],
        }
    )
    assert "要找 5 部，目前只找到 1 部" in reply
    assert "没有拿不符合条件的资源凑数" in reply
    assert "| # | 作品 | TMDB | M-Team 资源 |" in reply


def test_download_dispatch_is_idempotent_and_only_reports_verified_start(monkeypatch):
    class DownloadMTeam:
        def download_torrent_file(self, _torrent_id):
            return {"filename": "movie.torrent", "content": b"torrent"}

    class DownloadQb:
        add_calls = 0
        list_calls = 0

        def add_torrent_file(self, *_args):
            self.add_calls += 1
            return {"accepted": True, "trace_id": "DL-idempotent"}

        def get_torrents(self, _downloader_id, _filters=None):
            self.list_calls += 1
            if self.list_calls == 1:
                return []
            return [{"hash": "task-hash", "name": "电影版本", "progress": 0.1, "state": "queuedDL"}]

    qb = DownloadQb()
    monkeypatch.setattr(routes, "resolve_agent_downloader", lambda _db, _requested=None: "qb1")
    monkeypatch.setattr(routes, "get_mteam_adapter_or_error", lambda _db: DownloadMTeam())
    monkeypatch.setattr(routes, "get_qb_adapter_or_error", lambda _db, _downloader_id: qb)
    monkeypatch.setattr(routes, "save_qb_task_metadata", lambda *_args, **_kwargs: ["task-hash"])
    request = WechatClawMessageRequest(message="确认", user_id="idempotent", conversation_id="idempotent")
    candidate = {"id": "torrent-1", "title": "电影版本", "resolution": "1080p", "size": "10 GB"}
    with SessionLocal() as db:
        operation = routes.save_wechat_claw_pending_download(db, request, candidate, actor_user_id=None)
        first = routes.download_wechat_claw_selected_torrent(db, "torrent-1", SimpleNamespace(id=None), candidate, operation)
        second = routes.download_wechat_claw_selected_torrent(db, "torrent-1", SimpleNamespace(id=None), candidate, operation)
        db.refresh(operation)
    assert first["state"] == "started"
    assert second["state"] == "started"
    assert qb.add_calls == 1
    assert operation.state == "started"


def test_download_state_rejects_paused_and_error_but_accepts_real_download_states():
    assert routes.qb_download_state_started({"state": "pausedDL", "progress": 0.2}) is False
    assert routes.qb_download_state_started({"state": "error", "progress": 0.2}) is False
    assert routes.qb_download_state_started({"state": "metaDL", "progress": 0}) is True
    assert routes.qb_download_state_started({"state": "stalledDL", "progress": 0.2}) is True


def test_knowledge_reply_has_versioned_source_and_sensitive_task_hash_is_hidden():
    result = routes.search_knowledge("50% 促销", 1)
    reply = routes.format_knowledge_reply(result)
    assert "来源：" in reply and "更新：" in reply
    assert "task-hash-secret" not in json.dumps(
        routes.agent_safe_payload({"task_hash": "task-hash-secret", "state": "started"}),
        ensure_ascii=False,
    )


def test_only_cross_source_queries_trigger_the_long_running_progress_notice():
    assert routes.agent_request_needs_progress(
        "帮我找5部近三年的高分科幻电影，M-Team上要有50%或更好的资源"
    ) is True
    assert routes.agent_request_needs_progress("帮我搜一下痴迷") is False
