import pytest

from app.config import settings as settings_module
from app.auth.security import hash_password, verify_password
from app.utils.redaction import parse_raw_headers, redact_payload


def test_password_hash_roundtrip():
    password_hash = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", password_hash)
    assert not verify_password("wrong", password_hash)


def test_parse_raw_headers_and_redact():
    parsed = parse_raw_headers("Cookie: abc1234\nUser-Agent: PTMH\nAuthorization: Bearer secret9876")
    redacted = redact_payload(parsed)
    assert parsed["Cookie"] == "abc1234"
    assert redacted["Cookie"] == "saved ending 1234"
    assert redacted["Authorization"] == "saved ending 9876"
    assert redacted["User-Agent"] == "PTMH"


def test_corrupt_runtime_secret_file_fails_closed(tmp_path, monkeypatch):
    secret_file = tmp_path / "runtime-secrets.json"
    secret_file.write_text("{damaged", encoding="utf-8")
    monkeypatch.setenv("APP_RUNTIME_SECRETS_FILE", str(secret_file))

    with pytest.raises(RuntimeError, match="unreadable or damaged"):
        settings_module._load_or_create_runtime_secret("jwt_signing_key")

    assert secret_file.read_text(encoding="utf-8") == "{damaged"
