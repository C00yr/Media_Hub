from datetime import datetime, timedelta, timezone

from app.api import routes
from app.utils.time import parse_datetime, reset_client_timezone, set_client_timezone, system_datetime, system_time_context, utc_iso


def test_utc_iso_is_explicit_and_round_trips():
    value = datetime(2026, 7, 16, 10, 30)
    encoded = utc_iso(value)
    assert encoded == "2026-07-16T10:30:00Z"
    assert parse_datetime(encoded) == value.replace(tzinfo=timezone.utc)


def test_client_timezone_context_overrides_backend_default():
    token = set_client_timezone("America/New_York")
    try:
        localized = system_datetime("2026-07-16T10:30:00Z")
        assert localized.isoformat() == "2026-07-16T06:30:00-04:00"
    finally:
        reset_client_timezone(token)


def test_ai_time_context_is_explicit_and_uses_client_system_timezone():
    token = set_client_timezone("Asia/Shanghai")
    try:
        context = system_time_context()
        assert context["current_time"].endswith("+08:00")
        assert context["current_date"] == context["current_time"][:10]
        assert context["timezone"] == "Asia/Shanghai"
        assert context["utc_offset"] == "+08:00"
        assert context["timezone_source"] == "client_system"
    finally:
        reset_client_timezone(token)


def test_traffic_periods_follow_configured_system_timezone(monkeypatch):
    local_timezone = timezone(timedelta(hours=5, minutes=30))

    def local_time(value=None):
        source = value or datetime(2026, 7, 16, 18, 0, tzinfo=local_timezone)
        if source.tzinfo is None:
            source = source.replace(tzinfo=timezone.utc)
        return source.astimezone(local_timezone)

    monkeypatch.setattr(routes, "system_datetime", local_time)
    captured_at = datetime(2026, 7, 16, 20, 15)
    day = routes.traffic_period_start(captured_at, "day")
    hour = routes.traffic_period_start(captured_at, "hour")

    assert day.isoformat() == "2026-07-17T00:00:00+05:30"
    assert routes.traffic_period_label(day, "day") == "07/17"
    assert hour.isoformat() == "2026-07-17T01:00:00+05:30"
    assert routes.traffic_period_label(hour, "hour") == "07/17 01:00"


def test_ai_current_period_labels_do_not_reuse_stale_snapshot(monkeypatch):
    monkeypatch.setattr(
        routes,
        "system_datetime",
        lambda value=None: datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
    )
    stale = {
        "traffic_series": {
            "day": [{"label": "07/15", "upload_total": 5 * 1024**3}],
        }
    }
    current = {
        "traffic_series": {
            "day": [
                {"label": "07/15", "upload_total": 5 * 1024**3},
                {"label": "07/16", "upload_total": 2 * 1024**3},
            ],
        }
    }

    assert routes.latest_mteam_upload_label(stale, "day") == "暂无快照"
    assert routes.latest_mteam_upload_label(current, "day") == "2.00 GB"

def test_ai_reply_formats_update_time_in_current_user_timezone():
    token = set_client_timezone("America/New_York")
    try:
        reply = routes.format_mteam_station_reply(
            {
                "username": "member",
                "user_level": "User",
                "updated_at": "2026-07-16T10:30:00Z",
                "source": "M-Team 原始数据（Test）",
            }
        )
        assert "数据更新：2026-07-16 06:30" in reply
    finally:
        reset_client_timezone(token)
