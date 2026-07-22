from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.security import DEFAULT_CREDENTIALS_PENDING_KEY, decode_token
from app.db.session import get_db
from app.models.entities import Setting, User, UserSession
from app.utils.time import utc_now_naive


SESSION_LAST_SEEN_WRITE_INTERVAL = timedelta(minutes=5)


def invalid_session_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "auth_session_invalid", "message": "登录状态已失效，请重新登录。"},
    )


def token_expiry(payload: dict) -> datetime | None:
    value = payload.get("exp")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise invalid_session_exception()
    token = authorization.split(" ", 1)[1]
    try:
        payload = decode_token(token)
    except ValueError as exc:
        raise invalid_session_exception() from exc
    user = db.get(User, int(payload["sub"]))
    session = db.query(UserSession).filter(UserSession.token_id == payload["jti"]).one_or_none()
    if not user or user.role != "admin" or not user.is_active or not session:
        raise invalid_session_exception()
    now = utc_now_naive()
    if session.expires_at is None:
        session.expires_at = token_expiry(payload)
    if session.expires_at is not None and session.expires_at <= now:
        db.delete(session)
        db.commit()
        raise invalid_session_exception()
    pending = db.query(Setting).filter(Setting.key == DEFAULT_CREDENTIALS_PENDING_KEY).one_or_none()
    credentials_must_change = bool(pending and isinstance(pending.value, dict) and pending.value.get("required"))
    if credentials_must_change and request.url.path not in {"/api/auth/me", "/api/auth/account", "/api/auth/logout"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请先更新初始管理员账号和密码，再使用其他功能。")
    if session.last_seen_at is None or now - session.last_seen_at >= SESSION_LAST_SEEN_WRITE_INTERVAL:
        session.last_seen_at = now
    if db.is_modified(session):
        db.commit()
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return user


def has_qb2_grant(db: Session, authorization: str | None) -> bool:
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    try:
        payload = decode_token(authorization.split(" ", 1)[1])
    except ValueError:
        return False
    session = db.query(UserSession).filter(UserSession.token_id == payload["jti"]).one_or_none()
    return bool(session and session.qb2_grant_expires_at and session.qb2_grant_expires_at > utc_now_naive())
