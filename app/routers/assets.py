from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db, require_csrf
from app.models import Asset, BuildRecord, Host, User
from app.schemas import AssetCreate, AssetOut, AssetUpdate, BuildRecordOut


router = APIRouter(prefix="/api/assets", tags=["assets"])


def _validate_host(db: Session, host_id: int | None) -> None:
    if host_id is not None and not db.get(Host, host_id):
        raise HTTPException(status_code=400, detail="Host does not exist")


@router.get("", response_model=list[AssetOut])
def list_assets(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return db.scalars(select(Asset).order_by(Asset.created_at.desc())).all()


@router.post("", response_model=AssetOut, status_code=status.HTTP_201_CREATED)
def create_asset(
    payload: AssetCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    _validate_host(db, payload.host_id)
    asset = Asset(**payload.model_dump())
    db.add(asset)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Asset tag, serial number, or host is already used") from exc
    db.refresh(asset)
    return asset


@router.get("/{asset_id}", response_model=AssetOut)
def get_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@router.patch("/{asset_id}", response_model=AssetOut)
def update_asset(
    asset_id: int,
    payload: AssetUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    values = payload.model_dump(exclude_unset=True)
    _validate_host(db, values.get("host_id"))
    for key, value in values.items():
        setattr(asset, key, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Asset tag, serial number, or host is already used") from exc
    db.refresh(asset)
    return asset


@router.get("/{asset_id}/builds", response_model=list[BuildRecordOut])
def asset_builds(
    asset_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    if not db.get(Asset, asset_id):
        raise HTTPException(status_code=404, detail="Asset not found")
    return db.scalars(
        select(BuildRecord)
        .where(BuildRecord.asset_id == asset_id)
        .order_by(BuildRecord.started_at.desc(), BuildRecord.id.desc())
    ).all()

