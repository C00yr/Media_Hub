from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "PT Media Hub"
    app_version: str = "0.1.0-mvp"
    environment: str = "development"
    database_url: str = "sqlite:///./data/pt_media_hub.db"
    app_config_encryption_key: str = "dev-change-me-32-bytes-minimum"
    jwt_signing_key: str = "dev-change-me-jwt"
    jwt_expire_minutes: int = 60 * 24 * 7
    qb2_grant_minutes: int = 15
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    debug_trace_retention_days: int = 7
    snapshot_interval_minutes: int = 10
    static_dir: str = "static"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

