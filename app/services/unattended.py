from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppSetting, Host, HostState, Profile
from app.security import generate_install_token
from app.settings import settings


DEFAULT_PROFILE_SETTING = "unattended_default_profile"


def _setting(db: Session) -> AppSetting:
    setting = db.get(AppSetting, DEFAULT_PROFILE_SETTING)
    if setting:
        return setting
    setting = AppSetting(key=DEFAULT_PROFILE_SETTING, value_json={})
    db.add(setting)
    db.flush()
    return setting


def get_unattended_default_profile(db: Session) -> Profile | None:
    if settings.unattended_default_profile_name:
        profile = db.scalar(
            select(Profile).where(Profile.name == settings.unattended_default_profile_name).limit(1)
        )
        if profile:
            return profile

    setting = db.get(AppSetting, DEFAULT_PROFILE_SETTING)
    if setting:
        profile_id = setting.value_json.get("profile_id")
        if profile_id:
            profile = db.get(Profile, int(profile_id))
            if profile:
                return profile

    return db.scalar(select(Profile).order_by(Profile.created_at, Profile.id).limit(1))


def set_unattended_default_profile(db: Session, profile: Profile) -> None:
    setting = _setting(db)
    setting.value_json = {"profile_id": profile.id, "profile_name": profile.name}


def ensure_unattended_default_profile(db: Session) -> Profile | None:
    profile = get_unattended_default_profile(db)
    if profile:
        setting = db.get(AppSetting, DEFAULT_PROFILE_SETTING)
        if not setting or setting.value_json.get("profile_id") != profile.id:
            set_unattended_default_profile(db, profile)
    return profile


def is_unattended_default_profile(db: Session, profile: Profile) -> bool:
    default_profile = get_unattended_default_profile(db)
    return bool(default_profile and default_profile.id == profile.id)


def apply_unattended_profile(db: Session, host: Host) -> bool:
    if not settings.unattended_auto_enroll:
        return False
    if host.profile_id and host.state in {HostState.READY, HostState.INSTALLING}:
        return True
    if host.state not in {HostState.PENDING, HostState.READY}:
        return False

    profile = ensure_unattended_default_profile(db)
    if not profile:
        return False

    host.profile = profile
    host.state = HostState.READY
    host.install_token = generate_install_token()
    return True
