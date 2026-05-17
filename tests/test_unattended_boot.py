from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.database import Base
from app.models import Host, HostState, Image, OSType, Profile
from app.routers.boot import boot_ipxe
from app.security import generate_install_token
from app.services.unattended import set_unattended_default_profile
from app.settings import settings


def request(path: str = "/boot.ipxe") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def db_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'unattended.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    return Session(engine, autoflush=False, autocommit=False, expire_on_commit=False)


def add_default_profile(db: Session) -> Profile:
    image = Image(
        name="ubuntu-24-desktop",
        os_type=OSType.UBUNTU,
        architecture="x86_64",
        kernel_path="ubuntu/24/casper/vmlinuz",
        initrd_path="ubuntu/24/casper/initrd",
        metadata_json={},
    )
    profile = Profile(
        name="ubuntu-24-desktop",
        image=image,
        root_password="GeneratedPassword123",
        variables={},
    )
    db.add(profile)
    db.flush()
    set_unattended_default_profile(db, profile)
    db.commit()
    return profile


def configure_unattended(monkeypatch):
    monkeypatch.setattr(settings, "unattended_auto_enroll", True)
    monkeypatch.setattr(settings, "public_base_url", "http://pxe.test:9015")
    monkeypatch.setattr(settings, "files_base_url", "http://pxe.test:8080")


def test_unknown_host_gets_default_profile_and_install_script(tmp_path, monkeypatch):
    configure_unattended(monkeypatch)
    db = db_session(tmp_path)
    profile = add_default_profile(db)

    response = boot_ipxe(request(), mac="aa:bb:cc:dd:ee:ff", db=db)
    body = response.body.decode()
    host = db.scalar(select(Host).where(Host.mac == "aa:bb:cc:dd:ee:ff"))

    assert host is not None
    assert host.profile_id == profile.id
    assert host.state == HostState.READY
    assert "kernel http://pxe.test:8080/ubuntu/24/casper/vmlinuz" in body
    assert "ds=nocloud-net;s=http://pxe.test:9015/api/boot/seed/" in body


def test_pending_registered_host_is_promoted_to_unattended_install(tmp_path, monkeypatch):
    configure_unattended(monkeypatch)
    db = db_session(tmp_path)
    profile = add_default_profile(db)
    host = Host(
        mac="00:11:22:33:44:55",
        state=HostState.PENDING,
        install_token=generate_install_token(),
        variables={},
    )
    db.add(host)
    db.commit()

    response = boot_ipxe(request(), mac=host.mac, db=db)
    body = response.body.decode()
    db.refresh(host)

    assert host.profile_id == profile.id
    assert host.state == HostState.READY
    assert "kernel http://pxe.test:8080/ubuntu/24/casper/vmlinuz" in body
