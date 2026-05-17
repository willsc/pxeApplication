from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.models import User
from app.security import create_csrf_token


templates = Jinja2Templates(directory="templates")


def context(request: Request, user: User | None = None, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "request": request,
        "current_user": user,
        "csrf_token": create_csrf_token(user) if user else None,
    }
    data.update(extra)
    return data

