from __future__ import annotations

import secrets
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext

from app.models import User
from app.settings import settings


password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_context.verify(password, password_hash)


def generate_install_token() -> str:
    return secrets.token_urlsafe(24)


def generate_random_token(bytes_count: int = 16) -> str:
    return secrets.token_urlsafe(bytes_count)


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key.get_secret_value(), salt=salt)


def create_session_cookie(user: User) -> str:
    return _serializer("session").dumps({"uid": user.id, "username": user.username})


def load_session_cookie(value: str) -> dict[str, Any] | None:
    try:
        data = _serializer("session").loads(value, max_age=settings.session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or "uid" not in data:
        return None
    return data


def create_csrf_token(user: User) -> str:
    return _serializer("csrf").dumps({"uid": user.id, "nonce": secrets.token_urlsafe(16)})


def verify_csrf_token(token: str | None, user: User) -> bool:
    if not token:
        return False
    try:
        data = _serializer("csrf").loads(token, max_age=settings.csrf_max_age_seconds)
    except (BadSignature, SignatureExpired):
        return False
    return isinstance(data, dict) and data.get("uid") == user.id

