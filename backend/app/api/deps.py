from datetime import datetime

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.security import decode_token
from app.db.session import get_db
from app.models.entities import User, UserSession


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = decode_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    user = db.get(User, int(payload["sub"]))
    session = db.query(UserSession).filter(UserSession.token_id == payload["jti"]).one_or_none()
    if not user or user.role != "admin" or not user.is_active or not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive session")
    session.last_seen_at = datetime.utcnow()
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
    return bool(session and session.qb2_grant_expires_at and session.qb2_grant_expires_at > datetime.utcnow())
