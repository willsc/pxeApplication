from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Base, engine
from app.models import User
from app.security import hash_password
from app.services.profiles import ensure_default_profiles
from app.services.unattended import ensure_unattended_default_profile
from app.settings import settings

logger = logging.getLogger(__name__)


def init_database() -> None:
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        ensure_default_profiles(db)
        ensure_unattended_default_profile(db)
        if settings.initial_admin_username and settings.initial_admin_password:
            existing = db.scalar(select(User).where(User.username == settings.initial_admin_username))
            if not existing:
                user = User(
                    username=settings.initial_admin_username,
                    password_hash=hash_password(settings.initial_admin_password.get_secret_value()),
                )
                db.add(user)
                logger.info("Created initial admin user %s", settings.initial_admin_username)
        else:
            logger.warning("No PXE_INITIAL_ADMIN_PASSWORD set; create an admin with `pxe-admin create-admin`")
        db.commit()
