from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.models import BootEvent, Host
from app.services.ipxe import token_prefix


def record_event(
    db: Session,
    *,
    event_type: str,
    request: Request | None = None,
    host: Host | None = None,
    mac: str | None = None,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> BootEvent:
    event = BootEvent(
        host=host,
        mac=host.mac if host else mac,
        event_type=event_type,
        source_ip=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
        path=str(request.url.path) if request else None,
        token_prefix=token_prefix(token),
        payload=payload or {},
    )
    db.add(event)
    return event

