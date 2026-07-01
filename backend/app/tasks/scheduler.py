from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from sqlalchemy.orm import Session

from app.adapters.qbittorrent import QbittorrentWebAdapter
from app.adapters.mteam import MTeamAdapter
from app.db.session import SessionLocal
from app.models.entities import MTeamSnapshot, QbSnapshot
from app.services.integrations import get_config, get_decrypted_config


def capture_snapshots() -> None:
    db: Session = SessionLocal()
    try:
        mteam_row = get_config(db, "mteam")
        if mteam_row and mteam_row.enabled:
            try:
                stats = MTeamAdapter(get_decrypted_config(db, "mteam") or {}).get_user_stats()
                db.add(
                    MTeamSnapshot(
                        upload_total=stats["upload_total"],
                        download_total=stats["download_total"],
                        bonus=stats["bonus"],
                        ratio=stats["ratio"],
                        active_uploads=stats["active_uploads"],
                        active_downloads=stats["active_downloads"],
                        source="real",
                    )
                )
            except Exception:
                pass
        for downloader_id in ["qb1", "qb2", "qb3"]:
            row = get_config(db, downloader_id)
            if not row or not row.enabled or not row.encrypted_payload:
                continue
            try:
                state = QbittorrentWebAdapter(get_decrypted_config(db, downloader_id) or {}).get_server_state(downloader_id)
                db.add(
                    QbSnapshot(
                        downloader_id=downloader_id,
                        download_speed=state["download_speed"],
                        upload_speed=state["upload_speed"],
                        downloaded_total=state.get("downloaded_total", 0),
                        uploaded_total=state.get("uploaded_total", 0),
                        active_downloads=state["active_downloads"],
                        active_uploads=state["active_uploads"],
                        source="real",
                    )
                )
            except Exception:
                pass
        db.commit()
    finally:
        db.close()


def refresh_preload_caches() -> None:
    from app.api.routes import refresh_dashboard_preload, refresh_discover_preload, refresh_download_preload

    db: Session = SessionLocal()
    try:
        for refresh in (refresh_dashboard_preload, refresh_discover_preload):
            try:
                refresh(db)
            except Exception:
                db.rollback()
        for downloader_id in ["qb1", "qb2", "qb3"]:
            try:
                refresh_download_preload(db, downloader_id)
            except Exception:
                db.rollback()
    finally:
        db.close()


def build_scheduler(interval_minutes: int) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(capture_snapshots, "interval", minutes=interval_minutes, id="app_snapshots", replace_existing=True)
    scheduler.add_job(refresh_preload_caches, "interval", hours=1, id="preload_caches", replace_existing=True, next_run_time=datetime.utcnow())
    return scheduler
