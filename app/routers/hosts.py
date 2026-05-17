from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db, require_csrf
from app.models import BootEvent, BuildRecord, Host, HostState, Profile, User
from app.schemas import BootEventOut, BuildRecordOut, HostCreate, HostOut, HostUpdate
from app.security import generate_install_token


router = APIRouter(prefix="/api/hosts", tags=["hosts"])


def _profile_or_400(db: Session, profile_id: int | None) -> Profile | None:
    if profile_id is None:
        return None
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=400, detail="Profile does not exist")
    return profile


def _apply_profile(host: Host, profile_id: int | None) -> None:
    previous_profile_id = host.profile_id
    host.profile_id = profile_id
    if profile_id and host.state == HostState.PENDING:
        host.state = HostState.READY
        host.install_token = generate_install_token()
    elif previous_profile_id and profile_id is None and host.state == HostState.READY:
        host.state = HostState.PENDING
        host.install_token = generate_install_token()


@router.get("", response_model=list[HostOut])
def list_hosts(
    state: HostState | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    stmt = select(Host).order_by(Host.created_at.desc())
    if state:
        stmt = stmt.where(Host.state == state)
    return db.scalars(stmt).all()


@router.post("", response_model=HostOut, status_code=status.HTTP_201_CREATED)
def create_host(
    payload: HostCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    _profile_or_400(db, payload.profile_id)
    host = Host(
        mac=payload.mac,
        hostname=payload.hostname,
        profile_id=payload.profile_id,
        state=HostState.READY if payload.profile_id else HostState.PENDING,
        install_token=generate_install_token(),
        variables=payload.variables,
    )
    db.add(host)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Host MAC already exists") from exc
    db.refresh(host)
    return host


@router.get("/{host_id}", response_model=HostOut)
def get_host(
    host_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    host = db.get(Host, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    return host


@router.patch("/{host_id}", response_model=HostOut)
def update_host(
    host_id: int,
    payload: HostUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    host = db.get(Host, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    values = payload.model_dump(exclude_unset=True)
    if "profile_id" in values:
        _profile_or_400(db, values["profile_id"])
        _apply_profile(host, values.pop("profile_id"))
    for key, value in values.items():
        setattr(host, key, value)
        if key == "state":
            host.install_token = generate_install_token()
    db.commit()
    db.refresh(host)
    return host


@router.post("/{host_id}/request-install", response_model=HostOut)
def request_install(
    host_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    host = db.get(Host, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if not host.profile_id:
        raise HTTPException(status_code=400, detail="Assign a profile before requesting install")
    host.state = HostState.READY
    host.install_token = generate_install_token()
    host.provisioned_at = None
    db.commit()
    db.refresh(host)
    return host


@router.post("/{host_id}/decommission", response_model=HostOut)
def decommission_host(
    host_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    host = db.get(Host, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    host.state = HostState.DECOMMISSIONED
    host.install_token = generate_install_token()
    db.commit()
    db.refresh(host)
    return host


@router.get("/{host_id}/events", response_model=list[BootEventOut])
def host_events(
    host_id: int,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    if not db.get(Host, host_id):
        raise HTTPException(status_code=404, detail="Host not found")
    stmt = (
        select(BootEvent)
        .where(BootEvent.host_id == host_id)
        .order_by(BootEvent.created_at.desc())
        .limit(min(limit, 500))
    )
    return db.scalars(stmt).all()


@router.get("/{host_id}/builds", response_model=list[BuildRecordOut])
def host_builds(
    host_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    if not db.get(Host, host_id):
        raise HTTPException(status_code=404, detail="Host not found")
    stmt = (
        select(BuildRecord)
        .where(BuildRecord.host_id == host_id)
        .order_by(BuildRecord.started_at.desc(), BuildRecord.id.desc())
    )
    return db.scalars(stmt).all()
