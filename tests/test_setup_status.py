from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AppSetting, Image, OSType, Profile
from app.services.setup_status import (
    DHCP_PROBE_SETTING_KEY,
    build_recommendation,
    check_bootloaders,
    check_dhcp,
    check_distros,
    check_profiles,
    compute_setup_status,
    infer_dhcp_mode,
    parse_dnsmasq_conf,
)


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr("app.settings.settings.tftproot_dir", tmp_path / "tftproot")
    monkeypatch.setattr("app.settings.settings.dnsmasq_config_path", tmp_path / "dnsmasq.conf")
    monkeypatch.setattr("app.settings.settings.dhcp_mode", None)
    monkeypatch.setattr("app.settings.settings.pxe_network", None)
    monkeypatch.setattr("app.settings.settings.pxe_host_ip", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'setup.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session = Session(engine, autoflush=False, autocommit=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()


def test_check_bootloaders_reports_missing_files(tmp_path, monkeypatch):
    monkeypatch.setattr("app.settings.settings.tftproot_dir", tmp_path)
    status = check_bootloaders()
    assert not status.have_required
    assert set(status.missing_required) == {"undionly.kpxe", "ipxe.efi"}
    assert "snponly.efi" in status.missing_recommended
    assert not status.have_manifest


def test_check_bootloaders_recognizes_present_files(tmp_path, monkeypatch):
    monkeypatch.setattr("app.settings.settings.tftproot_dir", tmp_path)
    for name in ("undionly.kpxe", "ipxe.efi", "snponly.efi", "wimboot"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "bootloaders.json").write_text(json.dumps({"undionly.kpxe": {"sha256": "deadbeef"}}))
    status = check_bootloaders()
    assert status.have_required
    assert status.missing_required == []
    assert status.have_manifest
    assert "undionly.kpxe" in status.manifest


def test_parse_dnsmasq_conf_extracts_proxy_mode(tmp_path):
    conf = tmp_path / "dnsmasq.conf"
    conf.write_text(
        "# header\n"
        "dhcp-range=192.168.10.0,proxy,255.255.255.0\n"
        "dhcp-boot=tag:!ipxe,tag:efi64,ipxe.efi\n"
        "dhcp-boot=tag:ipxe,http://192.168.10.5:9000/boot.ipxe\n"
    )
    ranges, boots, network = parse_dnsmasq_conf(conf)
    assert ranges == ["192.168.10.0,proxy,255.255.255.0"]
    assert boots == [
        "tag:!ipxe,tag:efi64,ipxe.efi",
        "tag:ipxe,http://192.168.10.5:9000/boot.ipxe",
    ]
    assert network == "192.168.10.0"
    assert infer_dhcp_mode(ranges) == "proxy"


def test_infer_dhcp_mode_returns_server_for_full_range():
    ranges = ["192.168.10.100,192.168.10.200,255.255.255.0,12h"]
    assert infer_dhcp_mode(ranges) == "server"


def test_build_recommendation_flags_proxy_without_offers():
    rec = build_recommendation("proxy", detected_offers=[])
    assert rec is not None and "no dhcp server detected" in rec.lower()


def test_build_recommendation_flags_server_with_offers():
    rec = build_recommendation("server", detected_offers=[{"server_id": "192.168.10.1"}])
    assert rec is not None and "rerun" in rec.lower()


def test_check_distros_counts_imported_images(db):
    db.add(
        Image(
            name="ubuntu-26.04-desktop",
            os_type=OSType.UBUNTU,
            kernel_path="ubuntu/26-desktop/casper/vmlinuz",
            initrd_path="ubuntu/26-desktop/casper/initrd",
            metadata_json={"source": "ubuntu-release", "edition": "desktop", "version": "26.04"},
        )
    )
    db.add(
        Image(
            name="rocky-10",
            os_type=OSType.RHEL,
            kernel_path="rocky/10/vmlinuz",
            initrd_path="rocky/10/initrd.img",
            metadata_json={"source": "rocky-mirror", "version": "10"},
        )
    )
    db.commit()

    statuses, total = check_distros(db)
    by_slug = {s.distro.slug: s for s in statuses}
    assert total == 2
    assert by_slug["ubuntu-desktop"].image_count == 1
    assert by_slug["ubuntu-desktop"].versions_imported == ["26.04"]
    assert by_slug["rocky"].image_count == 1
    assert by_slug["ubuntu-server"].image_count == 0


def test_check_profiles_reports_default_and_orphaned_images(db):
    image = Image(
        name="rocky-10",
        os_type=OSType.RHEL,
        kernel_path="rocky/10/vmlinuz",
        initrd_path="rocky/10/initrd.img",
        metadata_json={"source": "rocky-mirror"},
    )
    db.add(image)
    db.flush()
    profile = Profile(name="rocky-10-default", image_id=image.id, variables={})
    db.add(profile)
    db.commit()

    status = check_profiles(db)
    assert status.profile_count == 1
    assert status.every_image_has_profile is True
    assert status.unattended_default is not None
    assert status.unattended_default.name == "rocky-10-default"


def test_check_dhcp_pulls_last_probe_from_appsetting(db, tmp_path, monkeypatch):
    conf = tmp_path / "dnsmasq.conf"
    conf.write_text("dhcp-range=10.0.0.0,proxy,255.255.255.0\n")
    monkeypatch.setattr("app.settings.settings.dnsmasq_config_path", conf)
    monkeypatch.setattr("app.settings.settings.dhcp_mode", None)
    db.add(
        AppSetting(
            key=DHCP_PROBE_SETTING_KEY,
            value_json={
                "checked_at": "2026-05-17T12:00:00+00:00",
                "offers": [{"server_id": "10.0.0.1", "offered_ip": "10.0.0.50"}],
            },
        )
    )
    db.commit()

    status = check_dhcp(db)
    assert status.configured_mode == "proxy"
    assert status.pxe_network == "10.0.0.0"
    assert status.detected_offers and status.detected_offers[0]["server_id"] == "10.0.0.1"
    # proxy + offers is the consistent case → no recommendation
    assert status.recommendation is None


def test_compute_setup_status_aggregates(db, tmp_path, monkeypatch):
    tftproot = tmp_path / "tftproot"
    tftproot.mkdir()
    for name in ("undionly.kpxe", "ipxe.efi"):
        (tftproot / name).write_bytes(b"x")
    monkeypatch.setattr("app.settings.settings.tftproot_dir", tftproot)
    monkeypatch.setattr("app.settings.settings.dnsmasq_config_path", tmp_path / "missing.conf")

    image = Image(
        name="ubuntu-26.04-server",
        os_type=OSType.UBUNTU,
        kernel_path="ubuntu/26-server/casper/vmlinuz",
        initrd_path="ubuntu/26-server/casper/initrd",
        metadata_json={"source": "ubuntu-release", "edition": "server", "version": "26.04"},
    )
    db.add(image)
    db.flush()
    profile = Profile(name="ubuntu-26-server", image_id=image.id, variables={})
    db.add(profile)
    db.commit()

    status = compute_setup_status(db)
    assert status.step1_done
    assert status.step2_done
    assert status.step3_done
    assert status.step4_ready
