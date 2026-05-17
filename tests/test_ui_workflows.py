from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.database import Base
from app.models import Host, HostState, Image, OSType, Profile, User
from app.routers.auth import logout, safe_next_url
from app.routers.ui import (
    create_image_form,
    create_profile_form,
    delete_image_form,
    delete_profile_form,
    image_detail_page,
    profile_detail_page,
    update_image_form,
    update_profile_form,
)
from app.security import generate_install_token, hash_password
from app.services.profiles import ensure_default_profile, ensure_default_profiles, is_auto_profile


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ui.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    with Session(engine, autoflush=False, autocommit=False, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def user(db: Session) -> User:
    operator = User(username="admin", password_hash=hash_password("VeryLongPassword123"))
    db.add(operator)
    db.commit()
    db.refresh(operator)
    return operator


def request(path: str = "/") -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


def create_image(db: Session, name: str = "ubuntu-24") -> Image:
    image = Image(name=name, os_type=OSType.UBUNTU, architecture="x86_64", metadata_json={})
    db.add(image)
    db.commit()
    db.refresh(image)
    return image


def test_safe_next_url_rejects_external_redirects():
    assert safe_next_url("https://evil.example/path") == "/"
    assert safe_next_url("//evil.example/path") == "/"
    assert safe_next_url("hosts") == "/"
    assert safe_next_url("/hosts") == "/hosts"


def test_logout_clears_session_cookie(user: User):
    response = logout(user)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "pxe_session=" in response.headers["set-cookie"]


def test_default_profile_is_created_for_each_image(db: Session):
    image = create_image(db)

    profile = ensure_default_profile(db, image)
    db.commit()

    assert profile.name == "ubuntu-24"
    assert profile.image_id == image.id
    assert profile.root_password
    assert profile.variables["timezone"] == "UTC"
    assert profile.variables["_pxe_app"]["managed"] == "auto-profile"
    assert is_auto_profile(profile)

    assert ensure_default_profiles(db) == 0
    assert db.query(Profile).count() == 1


def test_image_detail_update_and_delete(db: Session, user: User):
    create_response = create_image_form(
        request("/images"),
        name="ubuntu-24",
        os_type=OSType.UBUNTU,
        architecture="x86_64",
        kernel_path=None,
        initrd_path=None,
        repo_url=None,
        bootloader_path=None,
        wim_path=None,
        bcd_path=None,
        boot_sdi_path=None,
        extra_kernel_args=None,
        metadata_json="{}",
        db=db,
        user=user,
    )
    assert create_response.status_code == 303
    image = db.query(Image).filter_by(name="ubuntu-24").one()
    auto_profile = db.query(Profile).filter_by(image_id=image.id).one()
    assert is_auto_profile(auto_profile)

    page = image_detail_page(image.id, request(f"/images/{image.id}"), db=db, user=user)
    assert page.status_code == 200
    assert "Save image" in page.body.decode()

    bad_json = update_image_form(
        image.id,
        request(f"/images/{image.id}"),
        name="ubuntu-24",
        os_type=OSType.UBUNTU,
        architecture="x86_64",
        kernel_path=None,
        initrd_path=None,
        repo_url=None,
        bootloader_path=None,
        wim_path=None,
        bcd_path=None,
        boot_sdi_path=None,
        extra_kernel_args=None,
        metadata_json="{bad",
        db=db,
        user=user,
    )
    assert bad_json.status_code == 400
    assert "Image metadata must be valid JSON" in bad_json.body.decode()

    updated = update_image_form(
        image.id,
        request(f"/images/{image.id}"),
        name="ubuntu-24-renamed",
        os_type=OSType.UBUNTU,
        architecture="x86_64",
        kernel_path=None,
        initrd_path=None,
        repo_url=None,
        bootloader_path=None,
        wim_path=None,
        bcd_path=None,
        boot_sdi_path=None,
        extra_kernel_args=None,
        metadata_json='{"release": "24.04"}',
        db=db,
        user=user,
    )
    assert updated.status_code == 303
    assert db.get(Image, image.id).name == "ubuntu-24-renamed"

    deleted = delete_image_form(image.id, request(f"/images/{image.id}/delete"), db=db, user=user)
    assert deleted.status_code == 303
    assert db.get(Image, image.id) is None
    assert db.query(Profile).count() == 0


def test_image_delete_guard_when_profile_uses_image(db: Session, user: User):
    image = create_image(db)
    db.add(Profile(name="engineering", image=image, variables={}))
    db.commit()

    response = delete_image_form(image.id, request(f"/images/{image.id}/delete"), db=db, user=user)

    assert response.status_code == 409
    assert "Image is still used by profiles" in response.body.decode()


def test_profile_detail_validation_and_delete_guard(db: Session, user: User):
    image = create_image(db)
    create_response = create_profile_form(
        request("/profiles"),
        name="engineering",
        image_id=image.id,
        template_path=None,
        variables_json="{}",
        authorized_keys=None,
        root_password=None,
        db=db,
        user=user,
    )
    assert create_response.status_code == 303
    profile = db.query(Profile).filter_by(name="engineering").one()

    page = profile_detail_page(profile.id, request(f"/profiles/{profile.id}"), db=db, user=user)
    assert page.status_code == 200
    assert "Save profile" in page.body.decode()

    invalid_template = update_profile_form(
        profile.id,
        request(f"/profiles/{profile.id}"),
        name="engineering",
        image_id=image.id,
        template_path="../secret",
        variables_json="{}",
        authorized_keys=None,
        root_password=None,
        clear_root_password=None,
        db=db,
        user=user,
    )
    assert invalid_template.status_code == 400
    assert "Template path must be relative" in invalid_template.body.decode()

    db.add(
        Host(
            mac="aa:bb:cc:dd:ee:ff",
            profile=profile,
            state=HostState.READY,
            install_token=generate_install_token(),
            variables={},
        )
    )
    db.commit()

    delete_blocked = delete_profile_form(
        profile.id,
        request(f"/profiles/{profile.id}/delete"),
        db=db,
        user=user,
    )
    assert delete_blocked.status_code == 409
    assert "Profile is still assigned to hosts" in delete_blocked.body.decode()
