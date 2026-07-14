import json
import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _runtime_secret_file() -> Path:
    return Path(os.getenv("APP_RUNTIME_SECRETS_FILE", "/data/runtime-secrets.json"))


def _load_or_create_runtime_secret(name: str) -> str:
    secret_file = _runtime_secret_file()
    values: dict[str, str] = {}
    if secret_file.exists():
        try:
            values = json.loads(secret_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            values = {}
    value = values.get(name)
    if isinstance(value, str) and len(value) >= 32:
        return value
    value = secrets.token_urlsafe(48)
    values[name] = value
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # Last-resort fallback for read-only development environments. Docker/NAS
        # deployments persist this file under /data, so tokens remain stable.
        return value
    return value


class Settings(BaseSettings):
    app_name: str = "PT Media Hub"
    app_version: str = "0.1.0-mvp"
    environment: str = "development"
    database_url: str = "sqlite:///./data/pt_media_hub.db"
    app_config_encryption_key: str = Field(
        default_factory=lambda: _load_or_create_runtime_secret("app_config_encryption_key")
    )
    jwt_signing_key: str = Field(default_factory=lambda: _load_or_create_runtime_secret("jwt_signing_key"))
    jwt_expire_minutes: int = 60 * 24 * 7
    qb2_grant_minutes: int = 15
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    debug_trace_retention_days: int = 7
    snapshot_interval_minutes: int = 10
    static_dir: str = "static"
    tmdb_mode: str = "direct"
    tmdb_proxy_url: str = "http://mihomo:7890"
    tmdb_image_cache_dir: str = "/data/image-cache/tmdb"
    tmdb_image_cache_max_mb: int = 512

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
