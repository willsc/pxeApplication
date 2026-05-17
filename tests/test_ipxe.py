from __future__ import annotations

import pytest

from app.models import Host, HostState, Image, OSType, Profile
from app.services.ipxe import install_script, normalize_mac
from app.settings import Settings


def test_normalize_mac_accepts_common_formats():
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("aabb.ccdd.eeff") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"


def test_normalize_mac_rejects_invalid_values():
    with pytest.raises(ValueError):
        normalize_mac("not-a-mac")


def test_ubuntu_install_script_passes_iso_url_for_squashfs():
    settings = Settings(
        environment="test",
        secret_key="x" * 40,
        public_base_url="http://pxe.test:8000",
        files_base_url="http://pxe.test:8080",
    )
    image = Image(
        name="ubuntu-24",
        os_type=OSType.UBUNTU,
        kernel_path="ubuntu/24.04-server/casper/vmlinuz",
        initrd_path="ubuntu/24.04-server/casper/initrd",
        repo_url="ubuntu/24.04-server/ubuntu-24.04.4-live-server-amd64.iso",
    )
    profile = Profile(name="ubuntu", image=image)
    host = Host(
        mac="aa:bb:cc:dd:ee:ff",
        hostname="desk001",
        profile=profile,
        state=HostState.READY,
        install_token="token123",
    )

    script = install_script(host, settings).body

    assert "autoinstall" in script
    assert "ds=nocloud-net;s=http://pxe.test:8000/api/boot/seed/token123/" in script
    assert (
        "url=http://pxe.test:8080/ubuntu/24.04-server/ubuntu-24.04.4-live-server-amd64.iso"
        in script
    )


def test_rhel_install_script_contains_tokenized_config_url():
    settings = Settings(
        environment="test",
        secret_key="x" * 40,
        public_base_url="http://pxe.test:8000",
        files_base_url="http://pxe.test:8080",
    )
    image = Image(
        name="rocky",
        os_type=OSType.RHEL,
        kernel_path="rocky/vmlinuz",
        initrd_path="rocky/initrd.img",
        repo_url="http://mirror/rocky",
    )
    profile = Profile(name="linux", image=image)
    host = Host(
        mac="aa:bb:cc:dd:ee:ff",
        hostname="desk001",
        profile=profile,
        state=HostState.READY,
        install_token="token123",
    )

    script = install_script(host, settings).body

    assert "kernel http://pxe.test:8080/rocky/vmlinuz" in script
    assert "inst.ks=http://pxe.test:8000/api/boot/config/token123" in script
    assert "initrd http://pxe.test:8080/rocky/initrd.img" in script

