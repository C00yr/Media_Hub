import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import ConfigAuditLog, IntegrationConfig
from app.services.crypto import decrypt_text, encrypt_text
from app.utils.ids import trace_id
from app.utils.redaction import parse_raw_headers, redact_payload


PROVIDERS = ["mteam", "qb1", "qb2", "qb3", "tmdb", "ai"]


def normalize_payload(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    if provider == "mteam":
        raw_headers = normalized.pop("raw_headers", "")
        kv_headers = normalized.pop("headers", {}) or {}
        parsed = parse_raw_headers(raw_headers)
        normalized["headers"] = {**kv_headers, **parsed}
        for key in ["base_url", "api_base_url", "api_key", "passkey"]:
            if key in normalized and isinstance(normalized[key], str):
                normalized[key] = normalized[key].strip()
        if not normalized.get("base_url"):
            normalized["base_url"] = "https://kp.m-team.cc"
        if not normalized.get("timeout"):
            normalized["timeout"] = 10
        normalized = {key: value for key, value in normalized.items() if value not in ("", None, {})}
    if provider in {"qb1", "qb2", "qb3"}:
        for key in ["name", "base_url", "username", "password", "default_save_path"]:
            if key in normalized and isinstance(normalized[key], str):
                normalized[key] = normalized[key].strip()
        for key in ["category", "tags", "path_from", "path_to", "path_mappings", "nas_mount_paths", "storage_paths"]:
            normalized.pop(key, None)
        if not normalized.get("timeout"):
            normalized["timeout"] = 10
        normalized = {key: value for key, value in normalized.items() if value not in ("", None, [], {})}
    if provider == "tmdb":
        raw_settings = normalized.pop("raw_settings", "")
        if raw_settings:
            normalized.update(parse_raw_headers(raw_settings))
        if "bearer" in normalized and "bearer_token" not in normalized:
            normalized["bearer_token"] = normalized.pop("bearer")
        for key in ["api_key", "bearer_token", "language", "region", "endpoint", "mode", "proxy_url"]:
            if key in normalized and isinstance(normalized[key], str):
                normalized[key] = normalized[key].strip()
        for key in ["gateway_url", "gateway_key", "worker_name"]:
            normalized.pop(key, None)
        if normalized.get("mode") not in {"direct", "proxy"}:
            normalized["mode"] = "direct"
        if not normalized.get("proxy_url"):
            normalized["proxy_url"] = "http://mihomo:7890"
        if not normalized.get("language"):
            normalized["language"] = "zh-CN"
        if not normalized.get("region"):
            normalized["region"] = "CN"
        if not normalized.get("timeout"):
            normalized["timeout"] = 12
        normalized = {key: value for key, value in normalized.items() if value not in ("", None)}
    if provider == "ai":
        for key in ["base_url", "api_key", "model", "thinking", "reasoning_effort"]:
            if key in normalized and isinstance(normalized[key], str):
                normalized[key] = normalized[key].strip()
        if not normalized.get("base_url"):
            normalized["base_url"] = "https://api.deepseek.com"
        if not normalized.get("model"):
            normalized["model"] = "deepseek-v4-flash"
        if normalized.get("thinking") not in {"enabled", "disabled"}:
            normalized["thinking"] = "disabled"
        if normalized.get("reasoning_effort") not in {"high", "max"}:
            normalized["reasoning_effort"] = "high"
        if not normalized.get("timeout"):
            normalized["timeout"] = 30
        if not normalized.get("max_tokens"):
            normalized["max_tokens"] = 1200
        if not normalized.get("temperature"):
            normalized["temperature"] = 0.1
        normalized = {key: value for key, value in normalized.items() if value not in ("", None)}
    return normalized


def get_config(db: Session, provider: str) -> IntegrationConfig | None:
    return db.query(IntegrationConfig).filter(IntegrationConfig.provider == provider).one_or_none()


def get_decrypted_config(db: Session, provider: str) -> dict[str, Any] | None:
    row = get_config(db, provider)
    if not row or not row.encrypted_payload:
        return None
    return json.loads(decrypt_text(row.encrypted_payload))


def upsert_config(
    db: Session,
    provider: str,
    payload: dict[str, Any],
    actor_user_id: int,
    action: str = "save_draft",
) -> IntegrationConfig:
    normalized = normalize_payload(provider, payload)
    row = get_config(db, provider)
    if row is None:
        row = IntegrationConfig(provider=provider, config_version=1)
        db.add(row)
    else:
        row.config_version += 1
        if provider == "tmdb" and row.encrypted_payload:
            previous = json.loads(decrypt_text(row.encrypted_payload))
            for key in ("api_key", "bearer_token", "proxy_url"):
                if key not in normalized and previous.get(key):
                    normalized[key] = previous[key]
        if provider == "mteam" and row.encrypted_payload:
            previous = json.loads(decrypt_text(row.encrypted_payload))
            previous_headers = previous.get("headers", {})
            current_headers = normalized.get("headers", {})
            for key in ("Cookie", "Authorization", "User-Agent"):
                if key not in current_headers and previous_headers.get(key):
                    current_headers[key] = previous_headers[key]
            if current_headers:
                normalized["headers"] = current_headers
            for key in ("api_key", "passkey"):
                if key not in normalized and previous.get(key):
                    normalized[key] = previous[key]
        if provider in {"qb1", "qb2", "qb3"} and row.encrypted_payload:
            previous = json.loads(decrypt_text(row.encrypted_payload))
            if "password" not in normalized and previous.get("password"):
                normalized["password"] = previous["password"]
        if provider == "ai" and row.encrypted_payload:
            previous = json.loads(decrypt_text(row.encrypted_payload))
            if "api_key" not in normalized and previous.get("api_key"):
                normalized["api_key"] = previous["api_key"]
    row.encrypted_payload = encrypt_text(json.dumps(normalized, ensure_ascii=True))
    row.redacted_summary = redact_payload(normalized)
    row.last_tested_at = None
    row.last_test_result = None
    row.updated_at = datetime.utcnow()
    row.updated_by = actor_user_id
    row.enabled = row.enabled if normalized else False
    audit = ConfigAuditLog(
        provider=provider,
        config_version=row.config_version,
        action=action,
        test_success=None,
        actor_user_id=actor_user_id,
        trace_id=trace_id("CFG"),
    )
    db.add(audit)
    db.commit()
    db.refresh(row)
    return row


def record_test_result(
    db: Session,
    provider: str,
    result: dict[str, Any],
    actor_user_id: int,
) -> IntegrationConfig:
    row = get_config(db, provider)
    if row is None:
        row = IntegrationConfig(provider=provider, config_version=1, redacted_summary={})
        db.add(row)
    row.last_tested_at = datetime.utcnow()
    row.last_test_result = redact_payload(result)
    db.add(
        ConfigAuditLog(
            provider=provider,
            config_version=row.config_version,
            action="test",
            test_success=bool(result.get("success")),
            actor_user_id=actor_user_id,
            trace_id=str(result.get("trace_id") or trace_id("CFGTEST")),
        )
    )
    db.commit()
    db.refresh(row)
    return row


def set_enabled(db: Session, provider: str, enabled: bool, actor_user_id: int) -> IntegrationConfig:
    row = get_config(db, provider)
    if row is None:
        row = IntegrationConfig(provider=provider, config_version=1, redacted_summary={})
        db.add(row)
    row.enabled = enabled
    row.updated_at = datetime.utcnow()
    row.updated_by = actor_user_id
    db.add(
        ConfigAuditLog(
            provider=provider,
            config_version=row.config_version,
            action="enable" if enabled else "disable",
            test_success=None,
            actor_user_id=actor_user_id,
            trace_id=trace_id("CFG"),
        )
    )
    db.commit()
    db.refresh(row)
    return row


def serialize_config(row: IntegrationConfig, include_plain_payload: bool = False) -> dict[str, Any]:
    data = {
        "provider": row.provider,
        "config_version": row.config_version,
        "enabled": row.enabled,
        "redacted_summary": row.redacted_summary or {},
        "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
        "last_test_result": row.last_test_result,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if include_plain_payload:
        data["saved_payload"] = json.loads(decrypt_text(row.encrypted_payload)) if row.encrypted_payload else {}
    return data
