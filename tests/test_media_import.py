from __future__ import annotations

import argparse
import json

import pytest

from app.media_import import (
    IPXE_BOOT_BASE_URL,
    WIMBOOT_URL,
    MediaImportError,
    find_member,
    import_bootloaders,
    resolve_ubuntu_version,
    slugify,
    ubuntu_iso_name,
)


def test_ubuntu_iso_name_for_supported_editions():
    assert ubuntu_iso_name("26", "server") == "ubuntu-26.04-live-server-amd64.iso"
    assert ubuntu_iso_name("26", "desktop") == "ubuntu-26.04-desktop-amd64.iso"
    assert ubuntu_iso_name("24", "desktop") == "ubuntu-24.04.4-desktop-amd64.iso"
    assert ubuntu_iso_name("22", "desktop") == "ubuntu-22.04.5-desktop-amd64.iso"


def test_resolve_ubuntu_version_aliases():
    assert resolve_ubuntu_version("22") == "22.04.5"
    assert resolve_ubuntu_version("24.04") == "24.04.4"
    assert resolve_ubuntu_version("26") == "26.04"


def test_ubuntu_iso_name_rejects_unknown_edition():
    with pytest.raises(MediaImportError):
        ubuntu_iso_name("26.04", "minimal")


def test_find_member_matches_case_insensitively():
    members = ["SOURCES/BOOT.WIM", "boot/BCD", "./boot/boot.sdi"]

    assert find_member(members, ["sources/boot.wim"]) == "SOURCES/BOOT.WIM"
    assert find_member(members, ["efi/microsoft/boot/bcd", "boot/bcd"]) == "boot/BCD"


def test_slugify_keeps_safe_image_name():
    assert slugify("Windows 11 Enterprise") == "windows-11-enterprise"


def test_import_bootloaders_writes_chainload_files_and_manifest(tmp_path, monkeypatch):
    def fake_download(url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(url, encoding="utf-8")
        return destination

    monkeypatch.setattr("app.media_import.download_file", fake_download)
    args = argparse.Namespace(
        tftproot=str(tmp_path),
        ipxe_base_url=IPXE_BOOT_BASE_URL,
        wimboot_url=WIMBOOT_URL,
        include_wimboot=True,
    )

    assert import_bootloaders(args) == 0

    assert (tmp_path / "undionly.kpxe").exists()
    assert (tmp_path / "ipxe.efi").exists()
    assert (tmp_path / "snponly.efi").exists()
    assert (tmp_path / "ipxe32.efi").exists()
    assert (tmp_path / "wimboot").exists()
    assert (tmp_path / "windows" / "wimboot").exists()
    manifest = json.loads((tmp_path / "bootloaders.json").read_text(encoding="utf-8"))
    assert sorted(manifest) == [
        "ipxe.efi",
        "ipxe32.efi",
        "snponly.efi",
        "undionly.kpxe",
        "wimboot",
        "windows/wimboot",
    ]
    # x86_64-efi/ipxe.efi must be sourced from the UEFI subdir, not the legacy root.
    ipxe_efi_url = (tmp_path / "ipxe.efi").read_text(encoding="utf-8")
    assert ipxe_efi_url.endswith("/x86_64-efi/ipxe.efi"), ipxe_efi_url
