from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config.settings import get_settings
from app.db.session import Base, engine
from app.models import entities  # noqa: F401
from app.tasks.scheduler import build_scheduler, capture_snapshots


settings = get_settings()
scheduler = build_scheduler(settings.snapshot_interval_minutes)


def create_app() -> FastAPI:
    Base.metadata.create_all(bind=engine)
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
        capture_snapshots()
        if not scheduler.running:
            scheduler.start()

    @app.on_event("shutdown")
    def shutdown() -> None:
        if scheduler.running:
            scheduler.shutdown(wait=False)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": settings.app_version}

    static_path = Path(settings.static_dir)
    if static_path.exists():
        app.mount("/", StaticFiles(directory=static_path, html=True), name="static")
    return app


app = create_app()
