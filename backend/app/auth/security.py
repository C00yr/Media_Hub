from datetime import datetime, timedelta
from uuid import uuid4

import bcrypt
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.models.entities import User, UserSession
from app.utils.time import utc_now_naive


settings = get_settings()


def hash_password(password: str) -> str:
    password_bytes = password.encode("utf-8")[:72]
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    password_bytes = password.encode("utf-8")[:72]
    return bcrypt.checkpw(password_bytes, password_hash.encode("utf-8"))


def create_access_token(db: Session, user: User) -> str:
    token_id = uuid4().hex
    expires = utc_now_naive() + timedelta(minutes=settings.jwt_expire_minutes)
    session = UserSession(user_id=user.id, token_id=token_id)
    db.add(session)
    db.commit()
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "jti": token_id,
        "exp": expires,
    }
    return jwt.encode(payload, settings.jwt_signing_key, algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_signing_key, algorithms=["HS256"])
    except JWTError as exc:
        raise ValueError("Invalid token") from exc
