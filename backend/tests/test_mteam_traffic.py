import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["APP_CONFIG_ENCRYPTION_KEY"] = "test-key"
os.environ["JWT_SIGNING_KEY"] = "test-jwt"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.models.entities import DebugTrace, MTeamSnapshot, MTeamTrafficRollup

from app.tasks.scheduler import build_scheduler


def test_mteam_traffic_uses_local_hour_day_week_month_and_year_boundaries(monkeypatch):
    shanghai = ZoneInfo("Asia/Shanghai")

    def shanghai_time(value=None):
        source = value if value is not None else datetime(2026, 7, 17, 12, 0, tzinfo=shanghai)
        if source.tzinfo is None:
            source = source.replace(tzinfo=timezone.utc)
        return source.astimezone(shanghai)

    monkeypatch.setattr(routes, "system_datetime", shanghai_time)
    points = [
        {"captured_at": datetime(2026, 7, 12, 16, 10), "upload_total": 10, "download_total": 1},
        {"captured_at": datetime(2026, 7, 19, 15, 50), "upload_total": 20, "download_total": 2},
        {"captured_at": datetime(2026, 7, 19, 16, 10), "upload_total": 30, "download_total": 3},
    ]
    series = {dimension: routes.aggregate_traffic_points(points, dimension) for dimension in routes.TRAFFIC_DIMENSIONS}

    assert [item["label"] for item in series["hour"]] == ["07/13 00:00", "07/19 23:00", "07/20 00:00"]
    assert [item["label"] for item in series["day"]] == ["07/13", "07/19", "07/20"]
    assert [item["label"] for item in series["week"]] == ["07/13~07/19", "07/20~07/26"]
    assert [item["upload_total"] for item in series["week"]] == [30.0, 30.0]
    assert [(item["label"], item["upload_total"]) for item in series["month"]] == [("2026/07", 60.0)]
    assert [(item["label"], item["upload_total"]) for item in series["year"]] == [("2026", 60.0)]


def test_mteam_snapshot_history_is_not_truncated_to_5000_rows():
    engine = create_engine("sqlite:///:memory:")
    MTeamSnapshot.__table__.create(bind=engine)
    TestSession = sessionmaker(bind=engine)
    db = TestSession()
    try:
        start = datetime(2026, 1, 1)
        db.add_all(
            MTeamSnapshot(
                upload_total=float(index),
                download_total=float(index * 2),
                source="real",
                captured_at=start + timedelta(minutes=index),
            )
            for index in range(5002)
        )
        db.commit()

        points = routes.mteam_snapshot_delta_points(db)

        assert len(points) == 5001
        assert sum(item["upload_total"] for item in points) == 5001.0
        assert sum(item["download_total"] for item in points) == 10002.0
    finally:
        db.close()
        engine.dispose()

def test_mteam_snapshot_compaction_keeps_anchor_and_does_not_double_count(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    MTeamSnapshot.__table__.create(bind=engine)
    MTeamTrafficRollup.__table__.create(bind=engine)
    TestSession = sessionmaker(bind=engine)
    db = TestSession()
    shanghai = ZoneInfo("Asia/Shanghai")

    def shanghai_time(value=None):
        source = value if value is not None else datetime(2026, 7, 17, 12, 0, tzinfo=shanghai)
        if source.tzinfo is None:
            source = source.replace(tzinfo=timezone.utc)
        return source.astimezone(shanghai)

    monkeypatch.setattr(routes, "system_datetime", shanghai_time)
    monkeypatch.setattr(routes, "system_timezone_name", lambda: "Asia/Shanghai")
    monkeypatch.setattr(routes, "utc_now_naive", lambda: datetime(2026, 7, 17, 4, 0))
    try:
        db.add_all(
            [
                MTeamSnapshot(upload_total=100, download_total=20, source="real", captured_at=datetime(2026, 1, 1, 0, 0)),
                MTeamSnapshot(upload_total=130, download_total=25, source="real", captured_at=datetime(2026, 1, 1, 1, 0)),
                MTeamSnapshot(upload_total=180, download_total=35, source="real", captured_at=datetime(2026, 1, 2, 0, 0)),
                MTeamSnapshot(upload_total=300, download_total=50, source="real", captured_at=datetime(2026, 7, 10, 0, 0)),
            ]
        )
        db.commit()

        assert routes.compact_mteam_snapshots(db, retention_days=90) == 2
        assert db.query(MTeamSnapshot).count() == 2
        daily = db.query(MTeamTrafficRollup).filter(MTeamTrafficRollup.period_type == "day").all()
        monthly = db.query(MTeamTrafficRollup).filter(MTeamTrafficRollup.period_type == "month").all()
        assert sum(row.upload_total for row in daily) == 80
        assert sum(row.download_total for row in daily) == 15
        assert len(monthly) == 1
        assert monthly[0].upload_total == 80

        assert routes.compact_mteam_snapshots(db, retention_days=90) == 0
        assert db.query(MTeamTrafficRollup).filter(MTeamTrafficRollup.period_type == "month").one().upload_total == 80
    finally:
        db.close()
        engine.dispose()


def test_traffic_series_is_limited_by_real_time_windows(monkeypatch):
    shanghai = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 7, 17, 12, 30, tzinfo=shanghai)

    def shanghai_time(value=None):
        source = value if value is not None else now
        if source.tzinfo is None:
            source = source.replace(tzinfo=timezone.utc)
        return source.astimezone(shanghai)

    monkeypatch.setattr(routes, "system_datetime", shanghai_time)
    points = [
        {"captured_at": now - timedelta(hours=index), "upload_total": 1, "download_total": 1}
        for index in range(30)
    ]
    series = routes.aggregate_traffic_points(points, "hour")
    limited = routes.limit_traffic_series(series, "hour")

    assert len(limited) == 24
    assert limited[0]["label"] == "07/16 13:00"
    assert limited[-1]["label"] == "07/17 12:00"


def test_debug_trace_cleanup_enforces_retention(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    DebugTrace.__table__.create(bind=engine)
    TestSession = sessionmaker(bind=engine)
    db = TestSession()
    monkeypatch.setattr(routes, "utc_now_naive", lambda: datetime(2026, 7, 17, 4, 0))
    try:
        db.add_all(
            [
                DebugTrace(trace_id="old", event_type="test", created_at=datetime(2026, 7, 9, 3, 59)),
                DebugTrace(trace_id="new", event_type="test", created_at=datetime(2026, 7, 10, 4, 1)),
            ]
        )
        db.commit()

        assert routes.cleanup_debug_traces(db, retention_days=7) == 1
        assert [row.trace_id for row in db.query(DebugTrace).all()] == ["new"]
    finally:
        db.close()
        engine.dispose()


def test_scheduler_uses_snapshot_collection_for_mteam_and_qb_preloads():
    scheduler = build_scheduler(10)
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert jobs["app_snapshots"].func.__name__ == "capture_snapshots"
    assert jobs["content_preload_caches"].func.__name__ == "refresh_content_preload_caches"
    assert "preload_caches" not in jobs
