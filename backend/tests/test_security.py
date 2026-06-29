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

