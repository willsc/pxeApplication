from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db, require_csrf
from app.models import Host, Image, Profile, User
from app.schemas import ProfileCreate, ProfileOut, ProfileUpdate
from app.services.config_render import ConfigRenderError, _safe_template_path
from app.services.profiles import ensure_default_profiles


router = APIRouter(prefix="/api/profiles", tags=["profiles"])


def _validate_profile(db: Session, image_id: int | None, template_path: str | None) -> None:
    if image_id is not None and not db.get(Image, image_id):
        raise HTTPException(status_code=400, detail="Image does not exist")
    if template_path:
        try:
            _safe_template_path(template_path)
        except ConfigRenderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("", response_model=list[ProfileOut])
def list_profiles(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    if ensure_default_profiles(db):
        db.commit()
    return db.scalars(select(Profile).order_by(Profile.name)).all()


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
def create_profile(
    payload: ProfileCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    _validate_profile(db, payload.image_id, payload.template_path)
    profile = Profile(**payload.model_dump())
    db.add(profile)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Profile name already exists") from exc
    db.refresh(profile)
    return profile


@router.get("/{profile_id}", response_model=ProfileOut)
def get_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@router.patch("/{profile_id}", response_model=ProfileOut)
def update_profile(
    profile_id: int,
    payload: ProfileUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    values = payload.model_dump(exclude_unset=True)
    _validate_profile(db, values.get("image_id"), values.get("template_path"))
    for key, value in values.items():
        setattr(profile, key, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Profile name already exists") from exc
    db.refresh(profile)
    return profile


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    host_count = db.scalar(select(func.count(Host.id)).where(Host.profile_id == profile.id))
    if host_count:
        raise HTTPException(status_code=409, detail="Profile is still assigned to hosts")
    db.delete(profile)
    db.commit()
    return None
