from __future__ import annotations

import sys

import pytest

from app.services.media_jobs import (
    MediaJobError,
    bootloaders_command,
    debian_command,
    debian_set_command,
    rhel_family_command,
    rhel_family_set_command,
    ubuntu_command,
    ubuntu_set_command,
    windows_command,
)


def test_bootloader_job_uses_media_import_module():
    assert bootloaders_command() == [sys.executable, "-m", "app.media_import", "--replace", "bootloaders"]


def test_ubuntu_job_builds_single_iso_import_command():
    command = ubuntu_command(edition="desktop", version="24", name="ubuntu-workstation")

    assert command == [
        sys.executable,
        "-m",
        "app.media_import",
        "--replace",
        "ubuntu",
        "--edition",
        "desktop",
        "--version",
        "24",
        "--name",
        "ubuntu-workstation",
    ]


def test_ubuntu_set_job_builds_version_list_command():
    command = ubuntu_set_command(edition="server", versions=["22", "24", "26"])

    assert command == [
        sys.executable,
        "-m",
        "app.media_import",
        "--replace",
        "ubuntu-servers",
        "--versions",
        "22",
        "24",
        "26",
    ]


def test_windows_job_requires_url_or_iso():
    with pytest.raises(MediaJobError):
        windows_command()

    command = windows_command(url="https://example.test/win.iso", name="windows-11")
    assert command[-4:] == ["--url", "https://example.test/win.iso", "--name", "windows-11"]


def test_debian_job_builds_netboot_command():
    assert debian_command(version="trixie") == [
        sys.executable, "-m", "app.media_import", "--replace", "debian", "--version", "trixie",
    ]
    assert debian_set_command(versions=["bookworm", "trixie"]) == [
        sys.executable, "-m", "app.media_import", "--replace", "debian-set",
        "--versions", "bookworm", "trixie",
    ]


def test_rhel_family_jobs_build_per_family_commands():
    assert rhel_family_command("rocky", version="9") == [
        sys.executable, "-m", "app.media_import", "--replace", "rocky", "--version", "9",
    ]
    assert rhel_family_command("almalinux", version="10", name="alma-10") == [
        sys.executable, "-m", "app.media_import", "--replace",
        "almalinux", "--version", "10", "--name", "alma-10",
    ]
    assert rhel_family_set_command("fedora", versions=["43"]) == [
        sys.executable, "-m", "app.media_import", "--replace", "fedora-set", "--versions", "43",
    ]


def test_rhel_family_rejects_unknown_family():
    with pytest.raises(MediaJobError):
        rhel_family_command("centos", version="9")
    with pytest.raises(MediaJobError):
        rhel_family_set_command("centos", versions=["9"])
