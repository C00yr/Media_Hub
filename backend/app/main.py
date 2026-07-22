import logging
from contextlib import asynccontextmanager
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.auth.security import (
    DEFAULT_CREDENTIALS_PENDING_KEY,
    LEGACY_DEFAULT_ADMIN_PASSWORD,
    hash_password,
    verify_password,
)
from app.config.settings import get_settings
from app.db.migrations import upgrade_database
from app.db.session import SessionLocal, engine
from app.models.entities import Setting, User
from app.tasks.scheduler import build_scheduler, capture_snapshots, start_wechat_claw_polling, stop_wechat_claw_polling
from app.utils.time import reset_client_timezone, set_client_timezone, utc_now_naive


settings = get_settings()
scheduler = build_scheduler(settings.snapshot_interval_minutes)
logger = logging.getLogger(__name__)

SUPER_PASSWORD_SETTING_KEY = "auth.super_password"


def ensure_legacy_default_credentials_state() -> None:
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.role == "admin").order_by(User.id.asc()).first()
        if admin is None:
            return
        try:
            required = verify_password(LEGACY_DEFAULT_ADMIN_PASSWORD, admin.password_hash)
        except ValueError:
            required = False
        setting = db.query(Setting).filter(Setting.key == DEFAULT_CREDENTIALS_PENDING_KEY).one_or_none()
        current = bool(setting and isinstance(setting.value, dict) and setting.value.get("required"))
        if setting is None and required:
            db.add(Setting(key=DEFAULT_CREDENTIALS_PENDING_KEY, value={"required": True}))
            db.commit()
        elif setting is not None and current != required:
            setting.value = {"required": required}
            setting.updated_at = utc_now_naive()
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



@asynccontextmanager
async def app_lifespan(_: FastAPI):
    ensure_legacy_default_credentials_state()
    ensure_super_password()
    capture_snapshots()
    if not scheduler.running:
        scheduler.start()
    start_wechat_claw_polling()
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        stop_wechat_claw_polling()


def create_app() -> FastAPI:
    upgrade_database(engine, settings.database_url)
    ensure_legacy_default_credentials_state()
    ensure_super_password()
    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=app_lifespan)

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


    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": settings.app_version}

    static_path = Path(settings.static_dir)
    if static_path.exists():
        app.mount("/", StaticFiles(directory=static_path, html=True), name="static")
    return app


app = create_app()
