import logging
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from app.api.routes import router
from app.auth.security import hash_password
from app.config.settings import get_settings
from app.db.session import Base, SessionLocal, engine
from app.models import entities  # noqa: F401
from app.models.entities import Setting, User
from app.tasks.scheduler import build_scheduler, capture_snapshots, start_wechat_claw_polling, stop_wechat_claw_polling
from app.utils.time import reset_client_timezone, set_client_timezone


settings = get_settings()
scheduler = build_scheduler(settings.snapshot_interval_minutes)
logger = logging.getLogger(__name__)

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "adminadmin"
DEFAULT_CREDENTIALS_PENDING_KEY = "account.default_credentials_pending"
SUPER_PASSWORD_SETTING_KEY = "auth.super_password"


def ensure_default_admin() -> None:
    db = SessionLocal()
    try:
        if db.query(User).filter(User.role == "admin").first() is not None:
            return
        db.add(User(username=DEFAULT_ADMIN_USERNAME, password_hash=hash_password(DEFAULT_ADMIN_PASSWORD), role="admin"))
        db.add(Setting(key=DEFAULT_CREDENTIALS_PENDING_KEY, value={"required": True}))
        db.commit()
    finally:
        db.close()


def ensure_super_password() -> None:
    db = SessionLocal()
    try:
        setting = db.query(Setting).filter(Setting.key == SUPER_PASSWORD_SETTING_KEY).one_or_none()
        if setting and isinstance(setting.value, dict) and str(setting.value.get("password_hash") or ""):
            return
        super_password = secrets.token_urlsafe(24)
        value = {"password_hash": hash_password(super_password)}
        if setting is None:
            db.add(Setting(key=SUPER_PASSWORD_SETTING_KEY, value=value))
        else:
            setting.value = value
        db.commit()
        logger.warning("Recovery super password generated. Store it securely: %s", super_password)
    finally:
        db.close()


def ensure_schema_compatibility() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        if "mteam_snapshots" in tables:
            columns = {column["name"] for column in inspector.get_columns("mteam_snapshots")}
            if "user_level" not in columns:
                connection.execute(text("ALTER TABLE mteam_snapshots ADD COLUMN user_level VARCHAR(64) DEFAULT ''"))
            if "seed_size" not in columns:
                connection.execute(text("ALTER TABLE mteam_snapshots ADD COLUMN seed_size FLOAT DEFAULT 0"))
        binding_columns = set()
        if "wechat_claw_bindings" in tables:
            binding_columns = {column["name"] for column in inspector.get_columns("wechat_claw_bindings")}
        if binding_columns and "avatar_key" not in binding_columns:
            connection.execute(text("ALTER TABLE wechat_claw_bindings ADD COLUMN avatar_key VARCHAR(32) DEFAULT 'mint'"))


def create_app() -> FastAPI:
    Base.metadata.create_all(bind=engine)
    ensure_schema_compatibility()
    ensure_default_admin()
    ensure_super_password()
    app = FastAPI(title=settings.app_name, version=settings.app_version)

    @app.middleware("http")
    async def apply_client_timezone(request: Request, call_next):
        token = set_client_timezone(request.headers.get("X-Client-Timezone"))
        try:
            return await call_next(request)
        finally:
            reset_client_timezone(token)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router, prefix="/api")

    @app.on_event("startup")
    def startup() -> None:
        Base.metadata.create_all(bind=engine)
        ensure_schema_compatibility()
        ensure_default_admin()
        ensure_super_password()
        capture_snapshots()
        if not scheduler.running:
            scheduler.start()
        start_wechat_claw_polling()

    @app.on_event("shutdown")
    def shutdown() -> None:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        stop_wechat_claw_polling()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": settings.app_version}

    static_path = Path(settings.static_dir)
    if static_path.exists():
        app.mount("/", StaticFiles(directory=static_path, html=True), name="static")
    return app


app = create_app()
