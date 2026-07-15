from socket import timeout as SocketTimeout
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient

from app.adapters.ai.client import DeepSeekChatAdapter
from app.adapters.mteam.client import MTeamAdapter
from app.adapters.qbittorrent.client import QbittorrentWebAdapter
from app.adapters.tmdb import client as tmdb_client
from app.adapters.tmdb.client import TmdbAdapter, TmdbConfigError, TmdbDohError, TmdbImageError, tmdb_request_uses_proxy
from app.adapters.wechat_claw import WechatClawAdapter
from app.api import routes
from app.api.routes import classify_tmdb_gateway_test_error, classify_tmdb_test_error
from app.db.session import SessionLocal
from app.main import app
from app.services.integrations import normalize_payload


client = TestClient(app)


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_tmdb_missing_credentials_feedback():
    result = classify_tmdb_test_error(TmdbConfigError("missing"), "CFGTEST-1")
    assert result["success"] is False
    assert result["error_type"] == "missing_credentials"
    assert result["can_enable"] is False
    assert "Bearer Token" in result["next_step"]


def test_tmdb_http_error_feedback():
    unauthorized = classify_tmdb_test_error(HTTPError("https://api.themoviedb.org/3", 401, "Unauthorized", {}, None), "CFGTEST-2")
    assert unauthorized["error_type"] == "invalid_credentials"
    assert unauthorized["http_status"] == 401

    limited = classify_tmdb_test_error(HTTPError("https://api.themoviedb.org/3", 429, "Too Many Requests", {}, None), "CFGTEST-3")
    assert limited["error_type"] == "rate_limited"

    server_error = classify_tmdb_test_error(HTTPError("https://api.themoviedb.org/3", 500, "Server Error", {}, None), "CFGTEST-4")
    assert server_error["error_type"] == "tmdb_service_error"


def test_tmdb_network_feedback():
    timeout_result = classify_tmdb_test_error(SocketTimeout("timed out"), "CFGTEST-5")
    assert timeout_result["error_type"] == "timeout"

    network_result = classify_tmdb_test_error(URLError("dns failed"), "CFGTEST-6")
    assert network_result["error_type"] == "network_error"


def test_tmdb_doh_feedback():
    unavailable = classify_tmdb_test_error(TmdbDohError("doh refused", "doh_unavailable"), "CFGTEST-DOH1")
    assert unavailable["success"] is False
    assert unavailable["error_type"] == "doh_unavailable"

    bad_answer = classify_tmdb_test_error(TmdbDohError("bad answer", "doh_bad_answer"), "CFGTEST-DOH2")
    assert bad_answer["success"] is False
    assert bad_answer["error_type"] == "doh_bad_answer"


def test_tmdb_gateway_feedback_is_removed_compatibility():
    result = classify_tmdb_gateway_test_error(URLError("worker unreachable"), "CFGTEST-GW1", "health")
    assert result["success"] is False
    assert result["error_type"] == "gateway_removed"
    assert "不再支持" in result["message"]


def test_tmdb_gateway_payload_normalizes_to_direct():
    payload = normalize_payload("tmdb", {"mode": "gateway", "gateway_url": "https://example.workers.dev", "gateway_key": "secret", "bearer_token": "token"})
    assert payload["proxy_enabled"] is False
    assert payload["proxy_domains"] == ["api.themoviedb.org", "image.tmdb.org"]
    assert "gateway_url" not in payload
    assert "gateway_key" not in payload


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return b'{"images": {}, "change_keys": ["a"]}'


def test_tmdb_direct_uses_doh_opener(monkeypatch):
    calls = []

    def fake_doh(request, timeout):
        calls.append(("direct", request.full_url, timeout))
        return FakeResponse()

    monkeypatch.setattr(tmdb_client, "_urlopen_with_doh_ipv4", fake_doh)
    result = TmdbAdapter({"mode": "direct", "bearer_token": "token"}).test_connection()
    assert calls and calls[0][0] == "direct"
    assert result["network"]["network_mode"] == "direct"


def test_tmdb_proxy_uses_proxy_opener(monkeypatch):
    calls = []

    def fake_proxy(request, timeout):
        calls.append((getattr(request, "tmdb_proxy_url", ""), request.full_url, timeout))
        return FakeResponse()

    monkeypatch.setattr(tmdb_client, "_urlopen_with_proxy", fake_proxy)
    result = TmdbAdapter({"mode": "proxy", "bearer_token": "token", "proxy_url": "http://mihomo:7890"}).test_connection()
    assert calls and calls[0][0] == "http://mihomo:7890"
    assert result["network"]["network_mode"] == "selective_proxy"
    assert result["network"]["domain_routes"] == {
        "api.themoviedb.org": "proxy",
        "image.tmdb.org": "proxy",
    }


def test_tmdb_proxy_domain_policy_is_exact_and_selective():
    api_only = {"api.themoviedb.org"}
    assert tmdb_request_uses_proxy("https://api.themoviedb.org/3/movie/1", True, api_only) is True
    assert tmdb_request_uses_proxy("https://API.THEMOVIEDB.ORG:443/3/movie/1", True, api_only) is True
    assert tmdb_request_uses_proxy("https://image.tmdb.org/t/p/w92/a.jpg", True, api_only) is False
    assert tmdb_request_uses_proxy("https://evil.api.themoviedb.org.example/", True, api_only) is False
    assert tmdb_request_uses_proxy("https://api.themoviedb.org@evil.example/", True, api_only) is False
    assert tmdb_request_uses_proxy("https://[invalid/", True, api_only) is False
    assert tmdb_request_uses_proxy("https://api.themoviedb.org/3/movie/1", False, api_only) is False


def test_tmdb_network_dispatch_only_calls_proxy_for_selected_exact_host(monkeypatch):
    routes = []

    def fake_proxy(request, timeout):
        routes.append(("proxy", request.full_url, timeout))
        return object()

    def fake_direct(request, timeout):
        routes.append(("direct", request.full_url, timeout))
        return object()

    monkeypatch.setattr(tmdb_client, "_urlopen_with_proxy", fake_proxy)
    monkeypatch.setattr(tmdb_client, "_urlopen_with_doh_ipv4", fake_direct)
    selected = frozenset({"api.themoviedb.org"})

    tmdb_client.open_tmdb_network_request(Request("https://api.themoviedb.org/3/configuration"), True, "http://mihomo:7890", selected, 12)
    tmdb_client.open_tmdb_network_request(Request("https://image.tmdb.org/t/p/w92/a.jpg"), True, "http://mihomo:7890", selected, 12)
    tmdb_client.open_tmdb_network_request(Request("https://api.themoviedb.org.evil.example/"), True, "http://mihomo:7890", selected, 12)

    assert [route[0] for route in routes] == ["proxy", "direct", "direct"]


def test_tmdb_private_proxy_opener_has_a_second_policy_guard():
    request = Request("https://example.com/private")
    request.tmdb_proxy_url = "http://mihomo:7890"
    request.tmdb_proxy_domains = frozenset({"api.themoviedb.org"})

    with pytest.raises(URLError, match="proxy policy blocked host: example.com"):
        tmdb_client._urlopen_with_proxy(request, timeout=12)


def test_tmdb_proxy_payload_filters_unknown_domains_and_validates():
    payload = normalize_payload("tmdb", {
        "bearer_token": "token",
        "proxy_enabled": True,
        "proxy_url": "http://mihomo:7890",
        "proxy_domains": ["image.tmdb.org", "example.com"],
    })
    assert payload["proxy_domains"] == ["image.tmdb.org"]

    with pytest.raises(ValueError, match="至少选择一个"):
        normalize_payload("tmdb", {"proxy_enabled": True, "proxy_url": "http://mihomo:7890", "proxy_domains": []})
    with pytest.raises(ValueError, match="http://"):
        normalize_payload("tmdb", {"proxy_enabled": True, "proxy_url": "socks5://mihomo:7890", "proxy_domains": ["api.themoviedb.org"]})
    with pytest.raises(ValueError, match="http://"):
        normalize_payload("tmdb", {"proxy_enabled": True, "proxy_domains": ["api.themoviedb.org"]})


def test_tmdb_legacy_proxy_mode_selects_both_domains():
    payload = normalize_payload("tmdb", {"mode": "proxy", "proxy_url": "http://mihomo:7890", "bearer_token": "token"})
    assert payload["proxy_enabled"] is True
    assert payload["proxy_domains"] == ["api.themoviedb.org", "image.tmdb.org"]


def test_tmdb_environment_proxy_remains_a_fallback(monkeypatch):
    monkeypatch.setattr(
        tmdb_client,
        "get_settings",
        lambda: SimpleNamespace(tmdb_mode="proxy", tmdb_proxy_url="http://env-mihomo:7890"),
    )
    enabled, proxy_url, domains = tmdb_client.resolve_tmdb_proxy_settings({})
    assert enabled is True
    assert proxy_url == "http://env-mihomo:7890"
    assert domains == {"api.themoviedb.org", "image.tmdb.org"}


def test_tmdb_proxy_address_has_no_implicit_default(monkeypatch):
    monkeypatch.setattr(
        tmdb_client,
        "get_settings",
        lambda: SimpleNamespace(tmdb_mode="direct", tmdb_proxy_url=""),
    )
    enabled, proxy_url, domains = tmdb_client.resolve_tmdb_proxy_settings({})
    assert enabled is False
    assert proxy_url == ""
    assert domains == {"api.themoviedb.org", "image.tmdb.org"}

    with pytest.raises(TmdbConfigError, match="http://"):
        tmdb_client.resolve_tmdb_proxy_settings({"proxy_enabled": True, "proxy_domains": ["api.themoviedb.org"]})


def test_tmdb_proxy_blocks_cross_host_redirects():
    handler = tmdb_client._SameHostProxyRedirectHandler("api.themoviedb.org")
    with pytest.raises(URLError, match="Cross-host proxy redirect blocked"):
        handler.redirect_request(
            Request("https://api.themoviedb.org/3/configuration"),
            None,
            302,
            "Found",
            {},
            "https://image.tmdb.org/t/p/w92/poster.jpg",
        )


def test_tmdb_test_connection_reports_image_host_failure(monkeypatch):
    def fake_get(self, path, params):
        if path == "/configuration":
            return {"images": {}, "change_keys": ["a"]}
        return {"results": [{"poster_path": "/poster.jpg"}]}

    def fake_open(*_args, **_kwargs):
        raise URLError("image blocked")

    monkeypatch.setattr(TmdbAdapter, "_get", fake_get)
    monkeypatch.setattr(tmdb_client, "open_tmdb_network_request", fake_open)

    try:
        TmdbAdapter({"mode": "direct", "bearer_token": "token"}).test_connection()
    except TmdbImageError as exc:
        result = classify_tmdb_test_error(exc, "CFGTEST-IMG")
    else:
        raise AssertionError("Expected image host failure")

    assert result["success"] is False
    assert result["error_type"] == "tmdb_image_network_error"
    assert result["detail"]["image_probe"]["ok"] is False


def test_tmdb_discover_lists_are_lightweight(monkeypatch):
    calls = []

    def fake_get(self, path, params):
        calls.append(path)
        if path.startswith("/genre"):
            return {"genres": [{"id": 28, "name": "Action"}]}
        if path.startswith("/trending"):
            return {"results": [{"id": 1, "media_type": "movie", "title": "Trend", "poster_path": "/trend.jpg", "genre_ids": [28]}]}
        return {"results": [{"id": 2, "title": "Movie", "poster_path": "/movie.jpg", "genre_ids": [28]}]}

    monkeypatch.setattr(TmdbAdapter, "_get", fake_get)
    payload = TmdbAdapter({"mode": "direct", "bearer_token": "token"}).get_discover_lists()

    assert len([path for path in calls if not path.startswith("/genre")]) == 5
    assert not any(path in {"/movie/1", "/movie/2", "/tv/1", "/tv/2"} for path in calls)
    assert payload["trending"][0]["poster"].startswith("/api/tmdb/image/w342/")
    assert payload["trending"][0]["genres"] == ["Action"]

def test_tmdb_discover_genres_use_chinese_overrides_and_hide_tv_movie(monkeypatch):
    def fake_get(self, path, params):
        if path == "/genre/tv/list":
            return {
                "genres": [
                    {"id": 10765, "name": "Sci-Fi & Fantasy"},
                    {"id": 10768, "name": "War & Politics"},
                    {"id": 18, "name": "剧情"},
                ]
            }
        return {"genres": [{"id": 10770, "name": "电视电影"}, {"id": 28, "name": "动作"}]}

    monkeypatch.setattr(TmdbAdapter, "_get", fake_get)
    adapter = TmdbAdapter({"mode": "direct", "bearer_token": "token", "language": "zh-CN-filter-test"})
    options = adapter.get_discover_filter_options()

    assert options["genres"]["tv"] == [
        {"id": "10765", "name": "科幻与奇幻"},
        {"id": "10768", "name": "战争与政治"},
        {"id": "18", "name": "剧情"},
    ]
    assert options["genres"]["movie"] == [{"id": "28", "name": "动作"}]


def test_tmdb_discover_other_region_expands_to_unlisted_countries(monkeypatch):
    discover_params = []

    def fake_get(self, path, params):
        if path.startswith("/genre/"):
            return {"genres": []}
        discover_params.append(params)
        return {"page": 1, "total_pages": 1, "total_results": 0, "results": []}

    monkeypatch.setattr(TmdbAdapter, "_get", fake_get)
    adapter = TmdbAdapter({"mode": "direct", "bearer_token": "token", "language": "zh-CN-region-test"})
    adapter.discover_media({"media_type": "movie", "region": "OTHER", "include_options": False})

    countries = set(discover_params[0]["with_origin_country"].split("|"))
    assert {"CA", "AU", "ES", "IT", "BR"}.issubset(countries)
    assert countries.isdisjoint(tmdb_client.DISCOVER_PRIMARY_REGION_CODES)
    assert "region" not in discover_params[0]


class FakeImageHeaders:
    def get_content_type(self):
        return "image/jpeg"


class FakeImageResponse:
    headers = FakeImageHeaders()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return b"\xff\xd8\xff\xe0valid-jpeg-bytes"


def test_tmdb_image_proxy_validates_and_caches(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(routes.settings, "tmdb_image_cache_dir", str(tmp_path))
    monkeypatch.setattr(routes.settings, "tmdb_image_cache_max_mb", 1)

    def fake_open(request, proxy_enabled, proxy_url, proxy_domains, timeout):
        mode = "proxy" if tmdb_request_uses_proxy(request.full_url, proxy_enabled, proxy_domains) else "direct"
        calls.append((request.full_url, mode))
        return FakeImageResponse()

    monkeypatch.setattr(routes, "open_tmdb_network_request", fake_open)

    first = client.get("/api/tmdb/image/w342/poster.jpg")
    second = client.get("/api/tmdb/image/w342/poster.jpg")
    bad_size = client.get("/api/tmdb/image/bad/poster.jpg")
    bad_path = client.get("/api/tmdb/image/w342/../secret.jpg")

    assert first.status_code == 200
    assert first.content == b"\xff\xd8\xff\xe0valid-jpeg-bytes"
    assert second.status_code == 200
    assert len(calls) == 1
    assert bad_size.status_code == 400
    assert bad_path.status_code == 400


def test_tmdb_image_proxy_discards_invalid_cached_file(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(routes.settings, "tmdb_image_cache_dir", str(tmp_path))
    cache_file = routes._tmdb_image_cache_file("w342", "poster.jpg")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(b"<svg>No Image</svg>")

    def fake_open(request, proxy_enabled, proxy_url, proxy_domains, timeout):
        mode = "proxy" if tmdb_request_uses_proxy(request.full_url, proxy_enabled, proxy_domains) else "direct"
        calls.append((request.full_url, mode))
        return FakeImageResponse()

    monkeypatch.setattr(routes, "open_tmdb_network_request", fake_open)

    response = client.get("/api/tmdb/image/w342/poster.jpg")

    assert response.status_code == 200
    assert response.content == b"\xff\xd8\xff\xe0valid-jpeg-bytes"
    assert len(calls) == 1
    assert cache_file.read_bytes() == b"\xff\xd8\xff\xe0valid-jpeg-bytes"


def test_tmdb_image_proxy_uses_cached_file_magic_media_type(monkeypatch, tmp_path):
    monkeypatch.setattr(routes.settings, "tmdb_image_cache_dir", str(tmp_path))
    cache_file = routes._tmdb_image_cache_file("w342", "poster.jpg")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(b"RIFF\x10\x00\x00\x00WEBPvalid-webp-bytes")

    response = client.get("/api/tmdb/image/w342/poster.jpg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/webp")
    assert response.content.startswith(b"RIFF")


def test_preload_meta_rewrites_legacy_tmdb_image_urls():
    payload = {
        "items": [
            {
                "poster": "https://image.tmdb.org/t/p/w342/poster.jpg",
                "backdrop": "https://image.tmdb.org/t/p/w780/folder/backdrop.webp",
            }
        ]
    }

    result = routes.with_preload_meta(payload, {"cached_at": "2026-07-08T00:00:00"}, True)

    assert result["items"][0]["poster"] == "/api/tmdb/image/w342/poster.jpg"
    assert result["items"][0]["backdrop"] == "/api/tmdb/image/w780/folder/backdrop.webp"


def test_tmdb_image_proxy_returns_placeholder_when_upstream_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(routes.settings, "tmdb_image_cache_dir", str(tmp_path))

    def fake_open(*_args, **_kwargs):
        raise URLError("network failed")

    monkeypatch.setattr(routes, "open_tmdb_network_request", fake_open)

    response = client.get("/api/tmdb/image/w342/poster.jpg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["cache-control"] == "no-store"
    assert b"No Image" in response.content


def test_cached_discover_does_not_schedule_immediate_refresh(monkeypatch):
    created = client.post("/api/setup/admin", json={"username": "admin", "password": "password123"})
    if created.status_code == 200:
        token = created.json()["access_token"]
    else:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
        if login.status_code != 200:
            login = client.post("/api/auth/login", json={"username": "admin", "password": "adminadmin"})
        token = login.json()["access_token"]

    db = SessionLocal()
    try:
        configured = routes.enabled_tmdb_discover_config(db) is not None
        routes.set_preload_cache(
            db,
            "discover",
            {
                "source": "tmdb",
                "configured": configured,
                "trending": [],
                "popular_movies": [],
                "popular_tv": [],
                "top_rated_movies": [],
                "top_rated_tv": [],
            },
            context=routes.tmdb_discover_cache_context(db),
        )
    finally:
        db.close()

    def fail_if_scheduled(*_args, **_kwargs):
        raise AssertionError("cached discover should not schedule immediate refresh")

    monkeypatch.setattr(routes, "add_preload_task_once", fail_if_scheduled)
    response = client.get("/api/discover/lists?cached=true", headers=auth_headers(token))

    assert response.status_code == 200
    assert response.json()["_preload"]["preloaded"] is True


def test_discover_filter_without_tmdb_never_returns_mock_items(monkeypatch):
    monkeypatch.setattr(routes, "enabled_tmdb_discover_config", lambda _db: None)

    result = routes.build_discover_filter_payload(None, routes.default_discover_filter())

    assert result["source"] == "tmdb"
    assert result["configured"] is False
    assert result["items"] == []
    assert result["next_page"] is None


def test_non_tmdb_adapters_disable_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:7890")
    qb = QbittorrentWebAdapter({"base_url": "http://qb.local:8080", "username": "u", "password": "p"})
    mteam = MTeamAdapter({"api_key": "mteam-key"})
    ai = DeepSeekChatAdapter({"api_key": "ai-key"})
    wechat = WechatClawAdapter({})
    for adapter in (qb, mteam, ai, wechat):
        handler_names = [handler.__class__.__name__ for handler in adapter.opener.handlers]
        assert "ProxyHandler" not in handler_names


def test_tmdb_enable_requires_real_success():
    created = client.post("/api/setup/admin", json={"username": "admin", "password": "password123"})
    if created.status_code == 200:
        token = created.json()["access_token"]
    else:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
        if login.status_code != 200:
            login = client.post("/api/auth/login", json={"username": "admin", "password": "adminadmin"})
        token = login.json()["access_token"]

    saved = client.put(
        "/api/admin/integrations/tmdb",
        headers=auth_headers(token),
        json={"payload": {"api_key": "abc123", "language": "zh-CN", "region": "CN", "timeout": 12}},
    )
    assert saved.status_code == 200
    assert saved.json()["last_test_result"] is None

    enabled = client.post("/api/admin/integrations/tmdb/enable", headers=auth_headers(token))
    assert enabled.status_code == 409
