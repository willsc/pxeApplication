from __future__ import annotations

import pytest

from app.distros import CATALOG, by_slug, linux_distros, server_distros, desktop_distros
from app.models import OSType


def test_catalog_includes_all_supported_distros():
    slugs = {d.slug for d in CATALOG}
    assert slugs == {
        "ubuntu-desktop",
        "ubuntu-server",
        "debian",
        "rocky",
        "almalinux",
        "fedora",
        "windows",
    }


def test_default_versions_are_subset_of_versions():
    for distro in CATALOG:
        keys = {v.key for v in distro.versions}
        for default in distro.default_versions:
            assert default in keys, f"{distro.slug}: default {default} not in {keys}"


def test_by_slug_returns_distro_or_raises():
    assert by_slug("rocky").family == OSType.RHEL
    assert by_slug("debian").family == OSType.DEBIAN
    assert by_slug("ubuntu-server").role == "server"
    with pytest.raises(KeyError):
        by_slug("nope")


def test_role_filters_split_catalog_correctly():
    linux = {d.slug for d in linux_distros()}
    assert "windows" not in linux
    servers = {d.slug for d in server_distros()}
    assert "ubuntu-server" in servers
    assert "rocky" in servers
    desktops = {d.slug for d in desktop_distros()}
    assert "ubuntu-desktop" in desktops
    assert "windows" in desktops
