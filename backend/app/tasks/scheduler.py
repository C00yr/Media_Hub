from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app.adapters.mock import MockQbittorrentAdapter, MockTrackerAdapter
from app.db.session import SessionLocal
from app.models.entities import MTeamSnapshot, NasDiskSnapshot, QbSnapshot


def capture_mock_snapshots() -> None:
    db: Session = SessionLocal()
    try:
        tracker = MockTrackerAdapter()
        qb = MockQbittorrentAdapter()
        stats = tracker.get_user_stats()
        db.add(
            MTeamSnapshot(
                upload_total=stats["upload_total"],
                download_total=stats["download_total"],
                bonus=stats["bonus"],
                ratio=stats["ratio"],
                active_uploads=stats["active_uploads"],
                active_downloads=stats["active_downloads"],
                source="mock",
            )
        )
        for downloader_id in ["qb1", "qb2", "qb3"]:
            state = qb.get_server_state(downloader_id)
            db.add(
                QbSnapshot(
                    downloader_id=downloader_id,
                    download_speed=state["download_speed"],
                    upload_speed=state["upload_speed"],
                    downloaded_total=state["download_speed"] * 600,
                    uploaded_total=state["upload_speed"] * 600,
                    active_downloads=state["active_downloads"],
                    active_uploads=state["active_uploads"],
                    source="mock",
                )
            )
        db.add(NasDiskSnapshot(path_label="media", free_bytes=3.5 * 1024**4, total_bytes=8 * 1024**4, source="mock"))
        db.commit()
    finally:
        db.close()


def build_scheduler(interval_minutes: int) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(capture_mock_snapshots, "interval", minutes=interval_minutes, id="mock_snapshots", replace_existing=True)
    return scheduler

