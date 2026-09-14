import json
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlparse

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from app.models.entities import ConfigAuditLog, IntegrationConfig
from app.services.crypto import decrypt_text, encrypt_text
from app.utils.ids import trace_id
from app.utils.redaction import parse_raw_headers, redact_payload
from app.utils.time import utc_iso, utc_now_naive


PROVIDERS = ["mteam", "qb1", "qb2", "qb3", "tmdb", "ai", "wechat_claw"]
TMDB_PROXY_DOMAIN_ALLOWLIST = ("api.themoviedb.org", "image.tmdb.org")
TMDB_PROXY_DEFAULT_HOST = "tmdb-egress-proxy"
TMDB_PROXY_DEFAULT_PORT = 7890


def public_test_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    cleaned = dict(result)
    if cleaned.get("mode") in {"real", "mock", "config"}:
        cleaned.pop("mode", None)
    return cleaned


def decode_saved_payload(value: str | None) -> tuple[dict[str, Any], bool]:
    if not value:
        return {}, False
    try:
        return json.loads(decrypt_text(value)), False
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError):
        return {}, True


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
        proxy_password_supplied = "proxy_password" in normalized
        supplied_proxy_password = str(normalized.get("proxy_password") or "")
        raw_settings = normalized.pop("raw_settings", "")
        if raw_settings:
            normalized.update(parse_raw_headers(raw_settings))
        if "bearer" in normalized and "bearer_token" not in normalized:
            normalized["bearer_token"] = normalized.pop("bearer")
        for key in [
            "api_key",
            "bearer_token",
            "language",
            "region",
            "endpoint",
            "mode",
            "proxy_url",
            "proxy_scheme",
            "proxy_host",
            "proxy_username",
        ]:
            if key in normalized and isinstance(normalized[key], str):
                normalized[key] = normalized[key].strip()
        for key in ["gateway_url", "gateway_key", "worker_name"]:
            normalized.pop(key, None)
        if "proxy_enabled" in normalized:
            proxy_enabled = normalized.get("proxy_enabled") is True
        else:
            proxy_enabled = normalized.get("mode") == "proxy"
        legacy_proxy_url = str(normalized.get("proxy_url") or "").strip()
        if legacy_proxy_url and not normalized.get("proxy_host"):
            try:
                parsed_proxy = urlparse(legacy_proxy_url)
                parsed_port = parsed_proxy.port
            except ValueError as exc:
                raise ValueError("旧版代理地址无法解析，请重新填写代理主机和端口") from exc
            if parsed_proxy.scheme in {"http", "https"} and parsed_proxy.hostname:
                normalized["proxy_scheme"] = parsed_proxy.scheme
                normalized["proxy_host"] = parsed_proxy.hostname
                normalized["proxy_port"] = parsed_port or (443 if parsed_proxy.scheme == "https" else TMDB_PROXY_DEFAULT_PORT)
                if parsed_proxy.username and not normalized.get("proxy_username"):
                    normalized["proxy_username"] = unquote(parsed_proxy.username)
                if parsed_proxy.password and not normalized.get("proxy_password"):
                    normalized["proxy_password"] = unquote(parsed_proxy.password)
            else:
                raise ValueError("旧版代理地址无法解析，请重新填写代理主机和端口")
        normalized.pop("mode", None)
        normalized.pop("proxy_url", None)
        normalized.pop("proxy_domains", None)
        normalized["proxy_enabled"] = proxy_enabled
        proxy_scheme = str(normalized.get("proxy_scheme") or "http").lower()
        proxy_host = str(normalized.get("proxy_host") or TMDB_PROXY_DEFAULT_HOST).strip()
        try:
            proxy_port = int(normalized.get("proxy_port") or TMDB_PROXY_DEFAULT_PORT)
        except (TypeError, ValueError) as exc:
            raise ValueError("代理端口必须是 1 到 65535 之间的数字") from exc
        if proxy_scheme not in {"http", "https"}:
            raise ValueError("代理协议必须是 HTTP 或 HTTPS")
        if not proxy_host or any(character in proxy_host for character in "/?#@ "):
            raise ValueError("代理主机必须是有效的容器名称、域名或 IP 地址")
        if not 1 <= proxy_port <= 65535:
            raise ValueError("代理端口必须是 1 到 65535 之间的数字")
        normalized["proxy_scheme"] = proxy_scheme
        normalized["proxy_host"] = proxy_host
        normalized["proxy_port"] = proxy_port
        if proxy_enabled:
            if not proxy_host:
                raise ValueError("启用代理时必须填写代理主机")
            if normalized.get("proxy_password") and not normalized.get("proxy_username"):
                raise ValueError("填写代理密码时必须同时填写代理用户名")
        if not normalized.get("language"):
            normalized["language"] = "zh-CN"
        if not normalized.get("region"):
            normalized["region"] = "CN"
        if not normalized.get("timeout"):
            normalized["timeout"] = 12
        normalized = {key: value for key, value in normalized.items() if value not in ("", None)}
        if proxy_password_supplied:
            normalized["proxy_password"] = supplied_proxy_password
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
        try:
            normalized["timeout"] = max(90, min(300, int(normalized.get("timeout") or 90)))
        except (TypeError, ValueError):
            normalized["timeout"] = 90
        if not normalized.get("max_tokens"):
            normalized["max_tokens"] = 1200
        if not normalized.get("temperature"):
            normalized["temperature"] = 0.1
        normalized = {key: value for key, value in normalized.items() if value not in ("", None)}
    if provider == "wechat_claw":
        for key in ["mode", "name", "base_url", "default_target", "admin_user_ids", "public_base_url", "inbound_token", "webhook_url", "webhook_secret", "default_downloader_id"]:
            if key in normalized and isinstance(normalized[key], str):
                normalized[key] = normalized[key].strip()
        if normalized.get("mode") not in {"ilink", "direct"}:
            normalized["mode"] = "ilink"
        if not normalized.get("name"):
            normalized["name"] = "通知1"
        if not normalized.get("base_url"):
            normalized["base_url"] = "https://ilinkai.weixin.qq.com"
        if not normalized.get("poll_timeout"):
            normalized["poll_timeout"] = 25
        if not normalized.get("timeout"):
            normalized["timeout"] = 10
        if normalized.get("default_downloader_id") not in {"qb1", "qb2", "qb3", "all"}:
            normalized["default_downloader_id"] = "all"
        normalized = {key: value for key, value in normalized.items() if value not in ("", None)}
    return normalized


def get_config(db: Session, provider: str) -> IntegrationConfig | None:
    return db.query(IntegrationConfig).filter(IntegrationConfig.provider == provider).one_or_none()


def get_decrypted_config(db: Session, provider: str) -> dict[str, Any] | None:
    row = get_config(db, provider)
    if not row or not row.encrypted_payload:
        return None
    payload, unreadable = decode_saved_payload(row.encrypted_payload)
    return None if unreadable else payload


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
        previous, _ = decode_saved_payload(row.encrypted_payload)
        if provider == "tmdb":
            for key in ("api_key", "bearer_token", "proxy_password"):
                if key not in normalized and previous.get(key):
                    normalized[key] = previous[key]
        if provider == "mteam":
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
        if provider in {"qb1", "qb2", "qb3"}:
            if "password" not in normalized and previous.get("password"):
                normalized["password"] = previous["password"]
        if provider == "ai":
            if "api_key" not in normalized and previous.get("api_key"):
                normalized["api_key"] = previous["api_key"]
        if provider == "wechat_claw":
            for key in ("inbound_token", "webhook_secret"):
                if key not in normalized and previous.get(key):
                    normalized[key] = previous[key]
    row.encrypted_payload = encrypt_text(json.dumps(normalized, ensure_ascii=True))
    row.redacted_summary = redact_payload(normalized)
    row.last_tested_at = None
    row.last_test_result = None
    row.updated_at = utc_now_naive()
    row.updated_by = actor_user_id
    row.enabled = False if provider == "tmdb" else (row.enabled if normalized else False)
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
    row.last_tested_at = utc_now_naive()
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
    row.updated_at = utc_now_naive()
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
        "last_tested_at": utc_iso(row.last_tested_at) if row.last_tested_at else None,
        "last_test_result": public_test_result(row.last_test_result),
        "updated_at": utc_iso(row.updated_at) if row.updated_at else None,
    }
    if include_plain_payload:
        saved_payload, unreadable = decode_saved_payload(row.encrypted_payload)
        data["saved_payload"] = saved_payload
        data["configuration_unreadable"] = unreadable
    return data
