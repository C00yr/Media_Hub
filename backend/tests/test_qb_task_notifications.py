import hashlib

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


def test_qb_task_notifications_include_saved_mteam_metadata(monkeypatch):
    downloader_id = "qb1"
    info = b"d6:lengthi1024e4:name4:teste"
    torrent_content = b"d4:info" + info + b"e"
    task_hash = hashlib.sha1(info).hexdigest()
    deliveries: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        routes,
        "push_wechat_claw_notification",
        lambda _db, notification, action: deliveries.append((action, notification.title, notification.message)) or {"sent": True},
    )

    task = {
        "hash": task_hash,
        "name": "Example.Movie.2026.2160p.UHD.BluRay",
        "total_size": 8 * 1024**3,
        "state": "downloading",
        "progress": 0.1,
    }
    metadata = {
        "id": "12345",
        "subtitle": "示例电影 / 原盘中字",
        "promotion_label": "免费",
        "resolution": "2160p",
        "codec": "HEVC",
        "hdr": "Dolby Vision",
        "audio_codec": "TrueHD Atmos",
        "imdb_rating": "8.2",
        "douban_rating": "8.7",
    }

    with SessionLocal() as db:
        keys = [
            routes.qb_task_state_setting_key(downloader_id),
            routes.qb_task_metadata_setting_key(downloader_id),
        ]
        db.query(Setting).filter(Setting.key.in_(keys)).delete(synchronize_session=False)
        db.commit()

        assert routes.save_qb_task_metadata(db, downloader_id, torrent_content, metadata)[0] == task_hash
        assert routes.record_qb_task_transitions(db, downloader_id, []) == []
        assert routes.record_qb_task_transitions(db, downloader_id, [task]) == ["started"]
        task.update({"state": "uploading", "progress": 1})
        assert routes.record_qb_task_transitions(db, downloader_id, [task]) == ["completed"]

    assert [delivery[0] for delivery in deliveries] == ["qb1_download_started", "qb1_download_completed"]
    assert deliveries[0][1] == "qB1 下载器：下载已开始"
    assert deliveries[1][1] == "qB1 下载器：下载已完成"
    for _, _, message in deliveries:
        assert "任务：Example.Movie.2026.2160p.UHD.BluRay" in message
        assert "大小：8.0 GB" in message
        assert "示例电影 / 原盘中字" in message
        assert "优惠：免费" in message
        assert "清晰度：2160p · HEVC · Dolby Vision · TrueHD Atmos" in message
        assert "评分：IMDb 8.2 / 豆瓣 8.7" in message


def test_manual_qb_task_notification_uses_no_information_fallback():
    task = {
        "hash": "manual",
        "name": "Manual.Task",
        "total_size": 1024,
        "state": "downloading",
        "progress": 0.1,
    }

    notification = routes._qb_task_notification("qb1", "started", task)

    assert notification.title == "qB1 下载器：下载已开始"
    assert notification.message.splitlines() == ["任务：Manual.Task", "大小：1.0 KB", "描述：暂无信息"]
    assert "状态：" not in notification.message


def test_qb_download_notification_does_not_add_wechat_role_prefix():
    notification = Notification(
        title="qB1 下载器：下载已完成",
        message="任务：Example",
        source="qb_task_monitor",
    )

    assert routes.wechat_claw_notification_title(notification, "qb1_download_completed", "家庭影院助手") == notification.title
    assert routes.wechat_claw_notification_title(notification, "qb_exception", "家庭影院助手") == "【家庭影院助手】qB1 下载器：下载已完成"
