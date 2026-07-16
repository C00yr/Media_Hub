from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import datetime, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config.settings import get_settings


UTC = timezone.utc
_CLIENT_TIMEZONE: ContextVar[str | None] = ContextVar("client_timezone", default=None)


def _named_timezone(value: str | None) -> tzinfo | None:
    name = str(value or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


def set_client_timezone(value: str | None) -> Token[str | None]:
    normalized = str(value or "").strip()
    return _CLIENT_TIMEZONE.set(normalized if _named_timezone(normalized) else None)


def reset_client_timezone(token: Token[str | None]) -> None:
    _CLIENT_TIMEZONE.reset(token)


def system_timezone() -> tzinfo:
    return (
        _named_timezone(_CLIENT_TIMEZONE.get())
        or _named_timezone(get_settings().app_timezone)
        or datetime.now().astimezone().tzinfo
        or UTC
    )


def system_timezone_name() -> str:
    current = system_timezone()
    return str(getattr(current, "key", "") or current)


def utc_now() -> datetime:
    return datetime.now(UTC)


def system_now() -> datetime:
    return utc_now().astimezone(system_timezone())


def utc_now_naive() -> datetime:
    """UTC without tzinfo for the existing SQLite DateTime columns."""
    return utc_now().replace(tzinfo=None)


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_iso(value: datetime | None = None) -> str:
    normalized = utc_datetime(value or utc_now())
    return normalized.isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return utc_datetime(value)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return utc_datetime(parsed)


def system_datetime(value: datetime | str | int | float | None = None) -> datetime:
    parsed = parse_datetime(value) if value is not None else utc_now()
    return (parsed or utc_now()).astimezone(system_timezone())


def format_system_datetime(value: datetime | str | int | float | None, pattern: str = "%Y-%m-%d %H:%M") -> str:
    if value in (None, ""):
        return ""
    parsed = parse_datetime(value)
    return parsed.astimezone(system_timezone()).strftime(pattern) if parsed else ""


def epoch_seconds() -> int:
    return int(utc_now().timestamp())


def epoch_milliseconds() -> int:
    return int(utc_now().timestamp() * 1000)
