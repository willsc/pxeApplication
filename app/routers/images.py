from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db, require_csrf
from app.models import Image, User
from app.schemas import ImageCreate, ImageOut, ImageUpdate
from app.services.profiles import (
    blocking_profiles_for_image,
    delete_unassigned_auto_profiles,
    ensure_default_profile,
)
from app.services.unattended import ensure_unattended_default_profile


router = APIRouter(prefix="/api/images", tags=["images"])


@router.get("", response_model=list[ImageOut])
def list_images(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return db.scalars(select(Image).order_by(Image.name)).all()


@router.post("", response_model=ImageOut, status_code=status.HTTP_201_CREATED)
def create_image(
    payload: ImageCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    image = Image(**payload.model_dump())
    db.add(image)
    try:
        db.flush()
        ensure_default_profile(db, image)
        ensure_unattended_default_profile(db)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Image name already exists") from exc
    db.refresh(image)
    return image


@router.get("/{image_id}", response_model=ImageOut)
def get_image(
    image_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    image = db.get(Image, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    return image


@router.patch("/{image_id}", response_model=ImageOut)
def update_image(
    image_id: int,
    payload: ImageUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    image = db.get(Image, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(image, key, value)
    try:
        ensure_default_profile(db, image)
        ensure_unattended_default_profile(db)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Image name already exists") from exc
    db.refresh(image)
    return image


@router.delete("/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_image(
    image_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    image = db.get(Image, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    blocking_profiles = blocking_profiles_for_image(image)
    if blocking_profiles:
        raise HTTPException(status_code=409, detail="Image is still used by profiles")
    delete_unassigned_auto_profiles(db, image)
    db.delete(image)
    db.commit()
    return None
