import json
import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOCAL_DATA_DIR = PROJECT_ROOT / "data" / "local"


def _runtime_profile() -> str:
    return os.getenv("APP_RUNTIME_PROFILE", "local").strip().lower() or "local"


def _runtime_data_dir() -> Path:
    configured_path = os.getenv("APP_DATA_DIR")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path("/data") if _runtime_profile() == "nas" else LOCAL_DATA_DIR


def _default_database_url() -> str:
    return f"sqlite:///{(_runtime_data_dir() / 'pt_media_hub.db').resolve().as_posix()}"


def _runtime_secret_file() -> Path:
    configured_path = os.getenv("APP_RUNTIME_SECRETS_FILE")
    if configured_path:
        return Path(configured_path)
    return _runtime_data_dir() / "runtime-secrets.json"


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
    runtime_profile: str = Field(default_factory=_runtime_profile)
    app_data_dir: str = Field(default_factory=lambda: str(_runtime_data_dir()))
    database_url: str = Field(default_factory=_default_database_url)
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

    @model_validator(mode="after")
    def validate_runtime_storage_isolation(self) -> "Settings":
        profile = self.runtime_profile.strip().lower()
        if profile not in {"local", "nas"}:
            raise ValueError("APP_RUNTIME_PROFILE must be either 'local' or 'nas'.")

        allow_custom_storage = os.getenv("APP_ALLOW_CUSTOM_STORAGE", "").strip() == "1"
        data_dir = Path(self.app_data_dir).expanduser().resolve()
        if profile == "local" and not allow_custom_storage and data_dir != LOCAL_DATA_DIR.resolve():
            raise ValueError(
                f"Local development data must stay isolated in {LOCAL_DATA_DIR}. "
                "Remove APP_DATA_DIR/DATABASE_URL overrides or set APP_ALLOW_CUSTOM_STORAGE=1 intentionally."
            )

        if self.database_url == "sqlite:///:memory:":
            return self
        if self.database_url.startswith("sqlite:///"):
            raw_path = self.database_url.removeprefix("sqlite:///")
            database_path = Path(raw_path).expanduser()
            if not database_path.is_absolute():
                database_path = Path.cwd() / database_path
            expected_path = data_dir / "pt_media_hub.db"
            if not allow_custom_storage and database_path.resolve() != expected_path.resolve():
                raise ValueError(
                    f"DATABASE_URL must point to the {profile} profile database at {expected_path}. "
                    "This prevents local development and NAS from sharing one SQLite file."
                )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
