from app.api import routes
from app.db.session import SessionLocal
from app.models.entities import Notification, Setting


def test_qb_task_transitions_emit_once_after_the_initial_baseline(monkeypatch):
    downloader_id = "qb-transition-test"
    deliveries: list[str] = []
    monkeypatch.setattr(routes, "push_wechat_claw_notification", lambda _db, _notification, action: deliveries.append(action) or {"sent": True})

    def task(task_hash: str, state: str, progress: float) -> dict[str, object]:
        return {"hash": task_hash, "name": task_hash, "total_size": 1024, "state": state, "progress": progress}

    with SessionLocal() as db:
        db.query(Setting).filter(Setting.key == routes.qb_task_state_setting_key(downloader_id)).delete()
        db.commit()

        assert routes.record_qb_task_transitions(db, downloader_id, [task("existing", "downloading", 0.1)]) == []
        assert routes.record_qb_task_transitions(db, downloader_id, [task("existing", "downloading", 0.1)]) == []
        assert routes.record_qb_task_transitions(db, downloader_id, [task("existing", "downloading", 0.1), task("new", "downloading", 0.1)]) == ["started"]
        assert routes.record_qb_task_transitions(db, downloader_id, [task("existing", "downloading", 0.1), task("new", "uploading", 1)]) == ["completed"]
        assert routes.record_qb_task_transitions(db, downloader_id, [task("existing", "downloading", 0.1), task("new", "uploading", 1), task("broken", "downloading", 0.1)]) == ["started"]
        assert routes.record_qb_task_transitions(db, downloader_id, [task("existing", "downloading", 0.1), task("new", "uploading", 1), task("broken", "error", 0.1)]) == ["error"]

        notifications = db.query(Notification).filter(Notification.source == "qb_task_monitor").order_by(Notification.id.desc()).limit(4).all()

    assert deliveries == [
        f"{downloader_id}_download_started",
        f"{downloader_id}_download_completed",
        f"{downloader_id}_download_started",
        "qb_exception",
    ]
    assert len(notifications) == 4
