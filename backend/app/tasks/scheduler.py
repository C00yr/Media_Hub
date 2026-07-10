import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from sqlalchemy.orm import Session

from app.adapters.qbittorrent import QbittorrentWebAdapter
from app.adapters.mteam import MTeamAdapter
from app.db.session import SessionLocal
from app.models.entities import MTeamSnapshot, QbSnapshot
from app.services.integrations import get_config, get_decrypted_config


logger = logging.getLogger(__name__)
_WECHAT_CLAW_WORKER_LOCK = Lock()
_wechat_claw_worker: Thread | None = None
_wechat_claw_stop_event = Event()


def capture_snapshots() -> None:
    db: Session = SessionLocal()
    try:
        mteam_row = get_config(db, "mteam")
        if mteam_row and mteam_row.enabled:
            try:
                stats = MTeamAdapter(get_decrypted_config(db, "mteam") or {}).get_user_stats()
                db.add(
                    MTeamSnapshot(
                        user_level=stats.get("user_level") or "",
                        upload_total=stats["upload_total"],
                        download_total=stats["download_total"],
                        bonus=stats["bonus"],
                        ratio=stats["ratio"],
                        seed_size=stats.get("seed_size", 0),
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


def _wechat_claw_poll_loop() -> None:
    while not _wechat_claw_stop_event.is_set():
        db: Session | None = None
        delay_seconds = 3.0
        try:
            from app.api.routes import list_wechat_claw_binding_user_ids, poll_wechat_claw_messages

            db = SessionLocal()
            binding_user_ids = list_wechat_claw_binding_user_ids(db)
            db.close()
            db = None

            def poll_binding(user_id: int | None) -> dict:
                binding_db = SessionLocal()
                try:
                    return poll_wechat_claw_messages(binding_db, user_id)
                finally:
                    binding_db.close()

            with ThreadPoolExecutor(max_workers=min(max(len(binding_user_ids), 1), 8), thread_name_prefix="wechat-claw-binding") as executor:
                results = list(executor.map(poll_binding, binding_user_ids))
            result = next((item for item in results if item.get("success")), results[0])
            if result.get("retry_after_seconds") is not None:
                delay_seconds = max(0.2, min(float(result["retry_after_seconds"]), 5.0))
            elif result.get("success"):
                # A successful iLink long-poll either waited for a message or returned one.
                # Restart promptly so there is no idle gap between long-poll requests.
                delay_seconds = 0.1
            elif result.get("stage") in {"disabled", "login"}:
                delay_seconds = 5.0
            else:
                delay_seconds = 3.0
        except Exception:
            logger.exception("WeChat claw background polling failed")
            delay_seconds = 5.0
        finally:
            if db is not None:
                db.close()
        _wechat_claw_stop_event.wait(delay_seconds)


def start_wechat_claw_polling() -> None:
    global _wechat_claw_worker
    with _WECHAT_CLAW_WORKER_LOCK:
        if _wechat_claw_worker and _wechat_claw_worker.is_alive():
            return
        _wechat_claw_stop_event.clear()
        _wechat_claw_worker = Thread(target=_wechat_claw_poll_loop, name="wechat-claw-poll", daemon=True)
        _wechat_claw_worker.start()


def stop_wechat_claw_polling() -> None:
    global _wechat_claw_worker
    with _WECHAT_CLAW_WORKER_LOCK:
        _wechat_claw_stop_event.set()
        worker = _wechat_claw_worker
        _wechat_claw_worker = None
    if worker and worker.is_alive():
        worker.join(timeout=1.0)


def build_scheduler(interval_minutes: int) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(capture_snapshots, "interval", minutes=interval_minutes, id="app_snapshots", replace_existing=True)
    scheduler.add_job(refresh_preload_caches, "interval", hours=1, id="preload_caches", replace_existing=True, next_run_time=datetime.utcnow())
    return scheduler
