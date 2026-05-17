from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import User
from app.security import load_session_cookie, verify_csrf_token
from app.settings import settings


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    cookie = request.cookies.get(settings.session_cookie_name)
    if not cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    session = load_session_cookie(cookie)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
    user = db.get(User, int(session["uid"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive user")
    return user


def get_current_user_or_redirect(request: Request, db: Session = Depends(get_db)) -> User:
    try:
        return get_current_user(request, db)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": f"/login?next={request.url.path}"},
            ) from exc
        raise


async def require_csrf(
    request: Request,
    x_csrf_token: str | None = Header(default=None),
    user: User = Depends(get_current_user),
) -> User:
    token = x_csrf_token
    content_type = request.headers.get("content-type", "")
    if token is None and (
        content_type.startswith("application/x-www-form-urlencoded")
        or content_type.startswith("multipart/form-data")
    ):
        form = await request.form()
        form_value = form.get("csrf_token")
        token = str(form_value) if form_value is not None else None
    if not verify_csrf_token(token, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    return user


def redirect_to_login(next_path: str = "/") -> RedirectResponse:
    return RedirectResponse(f"/login?next={next_path}", status_code=status.HTTP_303_SEE_OTHER)

