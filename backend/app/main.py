from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from app.api.routes import router
from app.config.settings import get_settings
from app.db.session import Base, engine
from app.models import entities  # noqa: F401
from app.tasks.scheduler import build_scheduler, capture_snapshots, start_wechat_claw_polling, stop_wechat_claw_polling


settings = get_settings()
scheduler = build_scheduler(settings.snapshot_interval_minutes)


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
    app = FastAPI(title=settings.app_name, version=settings.app_version)
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
