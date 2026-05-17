from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db, require_csrf
from app.models import User
from app.security import create_csrf_token, create_session_cookie, verify_password
from app.settings import settings
from app.templating import context, templates


router = APIRouter(tags=["auth"])


def safe_next_url(value: str | None) -> str:
    if not value:
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    next = safe_next_url(next)
    return templates.TemplateResponse("login.html", context(request, next=next, error=None))


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    next = safe_next_url(next)
    user = db.scalar(select(User).where(User.username == username))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            context(request, next=next, error="Invalid username or password"),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = RedirectResponse(next or "/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        settings.session_cookie_name,
        create_session_cookie(user),
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=settings.session_max_age_seconds,
    )
    return response


@router.post("/logout")
def logout(_: User = Depends(require_csrf)):
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(settings.session_cookie_name)
    return response


@router.get("/api/auth/me")
def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "username": user.username, "csrf_token": create_csrf_token(user)}
