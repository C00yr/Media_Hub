from socket import timeout as SocketTimeout
from urllib.error import HTTPError, URLError

from fastapi.testclient import TestClient

from app.adapters.tmdb.client import TmdbConfigError, TmdbDohError
from app.api.routes import classify_tmdb_gateway_test_error, classify_tmdb_test_error
from app.main import app


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


def test_tmdb_gateway_worker_unreachable_feedback():
    result = classify_tmdb_gateway_test_error(URLError("worker unreachable"), "CFGTEST-GW1", "health")
    assert result["success"] is False
    assert result["error_type"] == "gateway_network_error"
    assert "Cloudflare Worker" in result["message"]


def test_tmdb_gateway_key_feedback():
    error = HTTPError("https://example.workers.dev/3/movie/popular", 403, "Forbidden", {"X-TMDB-Gateway-Error": "invalid_gateway_key"}, None)
    result = classify_tmdb_gateway_test_error(error, "CFGTEST-GW2")
    assert result["success"] is False
    assert result["error_type"] == "gateway_key_invalid"
    assert result["http_status"] == 403


def test_tmdb_gateway_tmdb_token_feedback():
    error = HTTPError("https://example.workers.dev/3/movie/popular", 401, "Unauthorized", {"X-TMDB-Gateway-Error": "tmdb_auth_error"}, None)
    result = classify_tmdb_gateway_test_error(error, "CFGTEST-GW3")
    assert result["success"] is False
    assert result["error_type"] == "gateway_tmdb_token_invalid"
    assert result["http_status"] == 401


def test_tmdb_gateway_missing_tmdb_token_feedback():
    error = HTTPError("https://example.workers.dev/3/movie/popular", 500, "Server Error", {"X-TMDB-Gateway-Error": "missing_tmdb_token"}, None)
    result = classify_tmdb_gateway_test_error(error, "CFGTEST-GW4")
    assert result["success"] is False
    assert result["error_type"] == "gateway_tmdb_token_missing"
    assert result["http_status"] == 500


def test_tmdb_enable_requires_real_success():
    created = client.post("/api/setup/admin", json={"username": "admin", "password": "password123"})
    token = created.json()["access_token"] if created.status_code == 200 else client.post("/api/auth/login", json={"username": "admin", "password": "password123"}).json()["access_token"]

    saved = client.put(
        "/api/admin/integrations/tmdb",
        headers=auth_headers(token),
        json={"payload": {"api_key": "abc123", "language": "zh-CN", "region": "CN", "timeout": 12}},
    )
    assert saved.status_code == 200
    assert saved.json()["last_test_result"] is None

    enabled = client.post("/api/admin/integrations/tmdb/enable", headers=auth_headers(token))
    assert enabled.status_code == 409
