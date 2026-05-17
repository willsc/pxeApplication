from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Image, OSType, Profile
from app.security import generate_random_token


AUTO_PROFILE_KEY = "_pxe_app"
AUTO_PROFILE_MANAGED = "auto-profile"


def is_auto_profile(profile: Profile) -> bool:
    marker = (profile.variables or {}).get(AUTO_PROFILE_KEY)
    return isinstance(marker, dict) and marker.get("managed") == AUTO_PROFILE_MANAGED


def default_profile_variables(image: Image) -> dict[str, Any]:
    variables: dict[str, Any] = {
        AUTO_PROFILE_KEY: {
            "managed": AUTO_PROFILE_MANAGED,
            "image_name": image.name,
            "version": 1,
        },
        "locale": "en_US.UTF-8",
        "keyboard": "us",
        "timezone": "UTC",
    }

    if image.os_type == OSType.RHEL:
        variables.update({"autopart_type": "lvm"})
    elif image.os_type == OSType.UBUNTU:
        variables.update(
            {
                "username": "operator",
                "storage_layout": "lvm",
                "allow_password_ssh": False,
            }
        )
    elif image.os_type == OSType.DEBIAN:
        variables.update(
            {
                "username": "operator",
                "domain": "local",
                "mirror_hostname": "deb.debian.org",
                "mirror_directory": "/debian",
            }
        )
    elif image.os_type == OSType.WINDOWS:
        variables.update(
            {
                "local_admin_username": "pxeadmin",
                "image_name": "Windows 11 Pro",
                "ui_language": "en-US",
                "input_locale": "en-US",
                "system_locale": "en-US",
                "user_locale": "en-US",
            }
        )

    return variables


def _bounded_name(base: str, suffix: str = "") -> str:
    max_base = 160 - len(suffix)
    return f"{base[:max_base].rstrip('-_. ')}{suffix}" or f"profile{suffix}"


def _unique_profile_name(db: Session, base_name: str) -> str:
    candidate = _bounded_name(base_name)
    if not db.scalar(select(Profile.id).where(Profile.name == candidate).limit(1)):
        return candidate

    candidate = _bounded_name(base_name, "-profile")
    if not db.scalar(select(Profile.id).where(Profile.name == candidate).limit(1)):
        return candidate

    index = 2
    while True:
        suffix = f"-profile-{index}"
        candidate = _bounded_name(base_name, suffix)
        if not db.scalar(select(Profile.id).where(Profile.name == candidate).limit(1)):
            return candidate
        index += 1


def ensure_default_profile(db: Session, image: Image) -> Profile:
    for profile in image.profiles:
        if is_auto_profile(profile):
            if not profile.root_password:
                profile.root_password = generate_random_token(18)
            return profile

    profile = Profile(
        name=_unique_profile_name(db, image.name),
        image=image,
        template_path=None,
        variables=default_profile_variables(image),
        root_password=generate_random_token(18),
    )
    db.add(profile)
    db.flush()
    return profile


def ensure_default_profiles(db: Session) -> int:
    created = 0
    images = db.scalars(select(Image).order_by(Image.name)).all()
    for image in images:
        before = len(image.profiles)
        ensure_default_profile(db, image)
        if len(image.profiles) > before:
            created += 1
    return created


def blocking_profiles_for_image(image: Image) -> list[Profile]:
    return [profile for profile in image.profiles if profile.hosts or not is_auto_profile(profile)]


def delete_unassigned_auto_profiles(db: Session, image: Image) -> None:
    for profile in list(image.profiles):
        if is_auto_profile(profile) and not profile.hosts:
            db.delete(profile)
