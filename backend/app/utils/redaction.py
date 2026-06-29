import re
from typing import Any

SENSITIVE_WORDS = ("cookie", "authorization", "password", "token", "api_key", "apikey", "passkey", "secret", "key")


def is_sensitive_key(key: str) -> bool:
    lower = key.lower().replace("-", "_")
    return any(word in lower for word in SENSITIVE_WORDS)


def mask_value(value: Any) -> str:
    if value is None:
        return "empty"
    text = str(value)
    if not text:
        return "empty"
    tail = text[-4:] if len(text) >= 4 else text
    return f"saved ending {tail}"


def redact_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: mask_value(value) if is_sensitive_key(str(key)) else redact_payload(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    if isinstance(payload, str):
        text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "[redacted-ip]", payload)
        text = re.sub(r"([A-Za-z]:)?[\\/](?:[^\\/\s]+[\\/])+[^\\/\s]+", "[redacted-path]", text)
        return text
    return payload


def parse_raw_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers

