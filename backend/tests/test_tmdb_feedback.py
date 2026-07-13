from socket import timeout as SocketTimeout
from urllib.error import HTTPError, URLError

from fastapi.testclient import TestClient

from app.adapters.mteam.client import MTeamAdapter
from app.adapters.qbittorrent.client import QbittorrentWebAdapter
from app.adapters.tmdb import client as tmdb_client
from app.adapters.tmdb.client import TmdbAdapter, TmdbConfigError, TmdbDohError, TmdbImageError
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
    assert "API Key" in result["next_step"]


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
    assert "Cloudflare Worker" in result["message"]


def test_tmdb_gateway_payload_normalizes_to_direct():
    payload = normalize_payload("tmdb", {"mode": "gateway", "gateway_url": "https://example.workers.dev", "gateway_key": "secret", "bearer_token": "token"})
    assert payload["mode"] == "direct"
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
    assert result["network"]["network_mode"] == "proxy"


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

    def fake_open(request, mode, proxy_url, timeout):
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

    def fake_open(request, mode, proxy_url, timeout):
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
        routes.set_preload_cache(db, "discover", {"configured": True, "trending": [], "popular_movies": [], "popular_tv": [], "top_rated_movies": [], "top_rated_tv": []})
    finally:
        db.close()

    def fail_if_scheduled(*_args, **_kwargs):
        raise AssertionError("cached discover should not schedule immediate refresh")

    monkeypatch.setattr(routes, "add_preload_task_once", fail_if_scheduled)
    response = client.get("/api/discover/lists?cached=true", headers=auth_headers(token))

    assert response.status_code == 200
    assert response.json()["_preload"]["preloaded"] is True


def test_qb_and_mteam_disable_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:7890")
    qb = QbittorrentWebAdapter({"base_url": "http://qb.local:8080", "username": "u", "password": "p"})
    mteam = MTeamAdapter({"api_key": "mteam-key"})
    qb_handler_names = [handler.__class__.__name__ for handler in qb.opener.handlers]
    mteam_handler_names = [handler.__class__.__name__ for handler in mteam.opener.handlers]
    assert "ProxyHandler" not in qb_handler_names
    assert "ProxyHandler" not in mteam_handler_names


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
