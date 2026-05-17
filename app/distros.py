"""Catalog of supported install media families.

Single source of truth shared by the media_import CLI, the media_jobs subprocess
builders, and the operator UI. Adding a new distro means appending an entry
here and (if needed) implementing the importer subcommand in
`app.media_import`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models import OSType


@dataclass(frozen=True)
class DistroVersion:
    key: str  # canonical token passed to the importer (e.g. "26", "9", "trixie")
    label: str  # human-readable label shown in the UI
    note: str = ""  # short qualifier ("LTS", "current stable", "EOL", ...)


@dataclass(frozen=True)
class Distro:
    slug: str  # stable identifier used in form values and URLs
    label: str  # display name ("Ubuntu Desktop")
    family: OSType
    role: str  # "desktop", "server", "workstation"
    description: str
    importer: str  # CLI subcommand under `python -m app.media_import`
    versions: tuple[DistroVersion, ...]
    default_versions: tuple[str, ...]  # versions selected by default in UI
    repo_hint: str = ""  # description of where install packages come from
    needs_iso: bool = False  # True if the importer downloads a multi-GB ISO
    iso_field_label: str = ""  # for Windows: human label on the URL field


UBUNTU_DESKTOP_VERSIONS = (
    DistroVersion("22", "Ubuntu 22.04 Desktop", "LTS, supported to 2027"),
    DistroVersion("24", "Ubuntu 24.04 Desktop", "LTS, supported to 2029"),
    DistroVersion("26", "Ubuntu 26.04 Desktop", "LTS, current"),
)
UBUNTU_SERVER_VERSIONS = (
    DistroVersion("22", "Ubuntu 22.04 Live Server", "LTS"),
    DistroVersion("24", "Ubuntu 24.04 Live Server", "LTS"),
    DistroVersion("26", "Ubuntu 26.04 Live Server", "LTS, current"),
)
DEBIAN_VERSIONS = (
    DistroVersion("bookworm", "Debian 12 (bookworm)", "oldstable"),
    DistroVersion("trixie", "Debian 13 (trixie)", "current stable"),
)
ROCKY_VERSIONS = (
    DistroVersion("9", "Rocky Linux 9", "LTS"),
    DistroVersion("10", "Rocky Linux 10", "current"),
)
ALMA_VERSIONS = (
    DistroVersion("9", "AlmaLinux 9", "LTS"),
    DistroVersion("10", "AlmaLinux 10", "current"),
)
FEDORA_VERSIONS = (
    DistroVersion("42", "Fedora 42", "stable"),
    DistroVersion("43", "Fedora 43", "current"),
)
WINDOWS_VERSIONS = (
    DistroVersion("windows-11", "Windows 11", "Microsoft official ISO"),
)


CATALOG: tuple[Distro, ...] = (
    Distro(
        slug="ubuntu-desktop",
        label="Ubuntu Desktop",
        family=OSType.UBUNTU,
        role="desktop",
        description="GNOME Desktop live ISO, autoinstall via cloud-init.",
        importer="ubuntu",
        versions=UBUNTU_DESKTOP_VERSIONS,
        default_versions=("22", "24", "26"),
        repo_hint="ISO served locally; squashfs streamed over HTTP.",
        needs_iso=True,
    ),
    Distro(
        slug="ubuntu-server",
        label="Ubuntu Server",
        family=OSType.UBUNTU,
        role="server",
        description="Live Server ISO with Subiquity autoinstall.",
        importer="ubuntu",
        versions=UBUNTU_SERVER_VERSIONS,
        default_versions=("22", "24", "26"),
        repo_hint="ISO served locally; squashfs streamed over HTTP.",
        needs_iso=True,
    ),
    Distro(
        slug="debian",
        label="Debian",
        family=OSType.DEBIAN,
        role="server",
        description="Debian-installer netboot, automated via preseed.",
        importer="debian",
        versions=DEBIAN_VERSIONS,
        default_versions=("trixie",),
        repo_hint="Packages streamed from a Debian HTTP mirror.",
    ),
    Distro(
        slug="rocky",
        label="Rocky Linux",
        family=OSType.RHEL,
        role="server",
        description="Anaconda netboot, automated via kickstart.",
        importer="rocky",
        versions=ROCKY_VERSIONS,
        default_versions=("9", "10"),
        repo_hint="Packages streamed from Rocky BaseOS mirror.",
    ),
    Distro(
        slug="almalinux",
        label="AlmaLinux",
        family=OSType.RHEL,
        role="server",
        description="Anaconda netboot, automated via kickstart.",
        importer="almalinux",
        versions=ALMA_VERSIONS,
        default_versions=("9", "10"),
        repo_hint="Packages streamed from AlmaLinux BaseOS mirror.",
    ),
    Distro(
        slug="fedora",
        label="Fedora",
        family=OSType.RHEL,
        role="workstation",
        description="Anaconda netboot for Fedora Everything.",
        importer="fedora",
        versions=FEDORA_VERSIONS,
        default_versions=("43",),
        repo_hint="Packages streamed from Fedora Everything mirror.",
    ),
    Distro(
        slug="windows",
        label="Windows 11",
        family=OSType.WINDOWS,
        role="desktop",
        description="Microsoft Windows 11 ISO via wimboot. Requires Microsoft-provided ISO URL or a local ISO.",
        importer="windows",
        versions=WINDOWS_VERSIONS,
        default_versions=(),
        repo_hint="Microsoft does not allow scraping; ISO must be supplied.",
        needs_iso=True,
        iso_field_label="Microsoft temporary ISO URL",
    ),
)


def by_slug(slug: str) -> Distro:
    for distro in CATALOG:
        if distro.slug == slug:
            return distro
    raise KeyError(f"Unknown distro slug: {slug}")


def linux_distros() -> tuple[Distro, ...]:
    return tuple(d for d in CATALOG if d.family != OSType.WINDOWS)


def server_distros() -> tuple[Distro, ...]:
    return tuple(d for d in CATALOG if d.role == "server")


def desktop_distros() -> tuple[Distro, ...]:
    return tuple(d for d in CATALOG if d.role in {"desktop", "workstation"})
