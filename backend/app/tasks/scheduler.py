import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app.adapters.qbittorrent import QbittorrentWebAdapter
from app.adapters.ai import DeepSeekChatAdapter
from app.adapters.tmdb import TmdbAdapter
from app.db.session import SessionLocal
from app.models.entities import QbSnapshot
from app.services.integrations import get_config, get_decrypted_config
from app.utils.time import parse_datetime, system_now, utc_datetime, utc_iso, utc_now


logger = logging.getLogger(__name__)
_WECHAT_CLAW_WORKER_LOCK = Lock()
_wechat_claw_worker: Thread | None = None
_wechat_claw_stop_event = Event()


def capture_snapshots() -> None:
    from app.api.routes import (
        cleanup_debug_traces,
        cleanup_expired_user_sessions,
        collect_mteam_snapshot,
        compact_mteam_snapshots,
        qb_placeholder_state,
        record_module_collection_result,
        record_qb_task_transitions,
        refresh_collected_preload_caches,
    )

    db: Session = SessionLocal()
    mteam_stats: dict | None = None
    mteam_error: str | None = None
    qbs: list[dict] = []
    downloads: dict[str, dict] = {}
    try:
        mteam_row = get_config(db, "mteam")
        if mteam_row and mteam_row.enabled:
            try:
                mteam_stats = collect_mteam_snapshot(db)
                record_module_collection_result(db, "mteam", True)
            except Exception as exc:
                mteam_error = str(exc)
                record_module_collection_result(db, "mteam", False, mteam_error)

        for downloader_id in ["qb1", "qb2", "qb3"]:
            row = get_config(db, downloader_id)
            if not row or not row.enabled or not row.encrypted_payload:
                qbs.append(qb_placeholder_state(db, downloader_id, row))
                continue
            try:
                adapter = QbittorrentWebAdapter(get_decrypted_config(db, downloader_id) or {})
                state = adapter.get_server_state(downloader_id)
                state.update({"configured": True, "enabled": True})
                tasks = adapter.get_torrents(downloader_id)
                tasks_captured_at = utc_iso()
                summary = dict(state)
                summary.update(adapter.summarize_torrents(tasks))
                downloads[downloader_id] = {
                    "downloader_id": downloader_id,
                    "summary": summary,
                    "items": tasks,
                    "source": "qB Web API",
                    "captured_at": state.get("captured_at"),
                    "checked_at": state.get("checked_at"),
                    "stale": False,
                    "tasks_captured_at": tasks_captured_at,
                    "updated_at": state.get("updated_at"),
                }
                qbs.append(state)
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
                        captured_at=utc_datetime(parse_datetime(state.get("captured_at")) or utc_now()).replace(tzinfo=None),
                    )
                )
                record_module_collection_result(db, downloader_id, True)
                record_qb_task_transitions(db, downloader_id, tasks)
            except Exception as exc:
                record_module_collection_result(db, downloader_id, False, str(exc))
                qbs.append(qb_placeholder_state(db, downloader_id, row, str(exc)))

        db.commit()
        compact_mteam_snapshots(db)
        cleanup_debug_traces(db)
        cleanup_expired_user_sessions(db)
        refresh_collected_preload_caches(
            db,
            mteam=mteam_stats,
            mteam_error=mteam_error,
            qbs=qbs,
            downloads=downloads,
        )
    except Exception:
        db.rollback()
        logger.exception("Scheduled snapshot collection failed")
    finally:
        db.close()


def check_external_module_health() -> None:
    """Run the non-snapshot integrations once per hour for sustained outage alerts."""
    db: Session = SessionLocal()
    try:
        from app.api.routes import record_module_collection_result

        for provider, factory in (("ai", DeepSeekChatAdapter), ("tmdb", TmdbAdapter)):
            row = get_config(db, provider)
            if not row or not row.enabled or not row.encrypted_payload:
                continue
            try:
                factory(get_decrypted_config(db, provider) or {}).test_connection()
                record_module_collection_result(db, provider, True)
            except Exception as exc:
                record_module_collection_result(db, provider, False, str(exc))
    finally:
        db.close()


def refresh_content_preload_caches() -> None:
    """Refresh TMDB content only; M-Team/qB caches are refreshed by capture_snapshots."""
    from app.api.routes import refresh_discover_preload

    db: Session = SessionLocal()
    try:
        refresh_discover_preload(db)
    except Exception:
        db.rollback()
        logger.exception("Scheduled TMDB preload refresh failed")
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
            if not binding_user_ids:
                delay_seconds = 5.0
                _wechat_claw_stop_event.wait(delay_seconds)
                continue

            def poll_binding(user_id: int | None) -> dict:
                binding_db = SessionLocal()
                try:
                    return poll_wechat_claw_messages(binding_db, user_id)
                finally:
                    binding_db.close()

            with ThreadPoolExecutor(max_workers=min(max(len(binding_user_ids), 1), 8), thread_name_prefix="wechat-claw-binding") as executor:
                results = list(executor.map(poll_binding, binding_user_ids))
            result = next((item for item in results if item.get("success")), results[0])
            from app.api.routes import record_module_collection_result
            monitor_db = SessionLocal()
            try:
                record_module_collection_result(monitor_db, "wechat_claw", bool(result.get("success")), str(result.get("message") or "WeChat claw polling failed"))
            finally:
                monitor_db.close()
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
    scheduler.add_job(check_external_module_health, "interval", hours=1, id="external_module_health", replace_existing=True, next_run_time=system_now())
    scheduler.add_job(refresh_content_preload_caches, "interval", hours=1, id="content_preload_caches", replace_existing=True, next_run_time=system_now())
    return scheduler
