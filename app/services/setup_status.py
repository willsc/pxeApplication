"""Compute the live setup status of the pxe-app stack.

Surfaces a structured view of:
- bootloaders present in tftproot
- imported OS media (per distro family)
- profiles + the chosen unattended default
- DHCP / network configuration: what is configured in dnsmasq.conf and env,
  and what the most-recent on-host DHCP probe (if any) detected.

The status is built from filesystem + DB state; the UI then renders it. The
status helpers never raise: missing files become explicit "missing" entries
rather than exceptions so the page still loads when setup is partial.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.distros import CATALOG, Distro
from app.models import AppSetting, Image, Profile
from app.services.unattended import get_unattended_default_profile
from app.settings import settings


REQUIRED_BOOTLOADERS = ("undionly.kpxe", "ipxe.efi")
RECOMMENDED_BOOTLOADERS = ("snponly.efi", "wimboot")
DHCP_PROBE_SETTING_KEY = "dhcp_probe_last"
DHCP_MODE_OVERRIDE_KEY = "dhcp_mode_override"
HOST_IP_OVERRIDE_KEY = "host_ip_override"


@dataclass
class BootloaderStatus:
    have_required: bool
    missing_required: list[str] = field(default_factory=list)
    missing_recommended: list[str] = field(default_factory=list)
    have_manifest: bool = False
    manifest: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class DistroStatus:
    distro: Distro
    image_count: int
    have_profile: bool
    versions_imported: list[str]


@dataclass
class ProfileStatus:
    profile_count: int
    unattended_default: Profile | None
    every_image_has_profile: bool


@dataclass
class DhcpStatus:
    # The "configured" mode: what the operator chose via setup.sh, persisted
    # in dnsmasq.conf and/or PXE_DHCP_MODE env.
    configured_mode: str | None
    pxe_network: str | None
    host_ip: str | None
    bootloader_directives: list[str] = field(default_factory=list)
    dhcp_range_directives: list[str] = field(default_factory=list)
    config_path: str | None = None
    config_found: bool = False
    # The "detected" mode: result of the most recent DHCP probe.
    detected_offers: list[dict[str, Any]] = field(default_factory=list)
    detected_at: str | None = None
    detection_error: str | None = None
    detection_running: bool = False
    detection_source: str | None = None
    sidecar_configured: bool = False
    recommendation: str | None = None


@dataclass
class SetupStatus:
    bootloaders: BootloaderStatus
    distros: list[DistroStatus]
    profiles: ProfileStatus
    dhcp: DhcpStatus
    images_total: int

    @property
    def step1_done(self) -> bool:
        return self.bootloaders.have_required

    @property
    def step2_done(self) -> bool:
        return self.images_total > 0

    @property
    def step3_done(self) -> bool:
        return (
            self.profiles.unattended_default is not None
            and self.profiles.every_image_has_profile
        )

    @property
    def step4_ready(self) -> bool:
        return self.step1_done and self.step2_done and self.step3_done


# ---------------------------------------------------------------------------
# Filesystem checks
# ---------------------------------------------------------------------------


def _tftproot() -> Path:
    root = settings.tftproot_dir
    if not root.is_absolute():
        root = Path.cwd() / root
    return root


def check_bootloaders() -> BootloaderStatus:
    root = _tftproot()
    missing_required = [name for name in REQUIRED_BOOTLOADERS if not (root / name).exists()]
    missing_recommended = [name for name in RECOMMENDED_BOOTLOADERS if not (root / name).exists()]
    manifest_path = root / "bootloaders.json"
    manifest: dict[str, dict[str, Any]] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
    return BootloaderStatus(
        have_required=not missing_required,
        missing_required=missing_required,
        missing_recommended=missing_recommended,
        have_manifest=manifest_path.exists(),
        manifest=manifest,
    )


# ---------------------------------------------------------------------------
# Distro mapping (image -> distro slug)
# ---------------------------------------------------------------------------


def _distro_for_image(image: Image) -> Distro | None:
    metadata = image.metadata_json or {}
    source = (metadata.get("source") or "").lower()
    edition = (metadata.get("edition") or "").lower()
    for distro in CATALOG:
        # Ubuntu rows share source="ubuntu-release"; split on edition.
        if source == "ubuntu-release":
            if distro.slug == "ubuntu-desktop" and edition == "desktop":
                return distro
            if distro.slug == "ubuntu-server" and edition == "server":
                return distro
            continue
        if source == "debian-netboot" and distro.slug == "debian":
            return distro
        if source.startswith("rocky") and distro.slug == "rocky":
            return distro
        if source.startswith("almalinux") and distro.slug == "almalinux":
            return distro
        if source.startswith("fedora") and distro.slug == "fedora":
            return distro
        if source == "windows-iso" and distro.slug == "windows":
            return distro
    return None


def check_distros(db: Session) -> tuple[list[DistroStatus], int]:
    images = db.scalars(select(Image)).all()
    by_slug: dict[str, list[Image]] = {distro.slug: [] for distro in CATALOG}
    for image in images:
        distro = _distro_for_image(image)
        if distro:
            by_slug[distro.slug].append(image)

    statuses: list[DistroStatus] = []
    for distro in CATALOG:
        distro_images = by_slug.get(distro.slug, [])
        versions = sorted(
            {
                (img.metadata_json or {}).get("version")
                or (img.metadata_json or {}).get("codename")
                or ""
                for img in distro_images
            }
            - {""}
        )
        statuses.append(
            DistroStatus(
                distro=distro,
                image_count=len(distro_images),
                have_profile=any(img.profiles for img in distro_images),
                versions_imported=versions,
            )
        )
    return statuses, len(images)


# ---------------------------------------------------------------------------
# Profile / unattended default checks
# ---------------------------------------------------------------------------


def check_profiles(db: Session) -> ProfileStatus:
    profile_count = db.scalar(select(func.count(Profile.id))) or 0
    unattended_default = get_unattended_default_profile(db)
    # Every image must have at least one profile.
    image_ids_without_profile = db.scalars(
        select(Image.id).where(~Image.profiles.any())
    ).all()
    return ProfileStatus(
        profile_count=profile_count,
        unattended_default=unattended_default,
        every_image_has_profile=not image_ids_without_profile,
    )


# ---------------------------------------------------------------------------
# DHCP / dnsmasq parsing
# ---------------------------------------------------------------------------


_RANGE_RE = re.compile(r"^\s*dhcp-range\s*=\s*(.+)$", re.IGNORECASE)
_BOOT_RE = re.compile(r"^\s*dhcp-boot\s*=\s*(.+)$", re.IGNORECASE)


def parse_dnsmasq_conf(path: Path) -> tuple[list[str], list[str], str | None]:
    """Return (range_directives, boot_directives, inferred_pxe_network)."""
    if not path.exists():
        return [], [], None
    ranges: list[str] = []
    boots: list[str] = []
    pxe_network: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _RANGE_RE.match(line)
        if match:
            ranges.append(match.group(1).strip())
            # First range's leading network token is "x.y.z.0,proxy,..." or
            # "x.y.z.100,x.y.z.200,255.255.255.0".
            tokens = [t.strip() for t in match.group(1).split(",")]
            if tokens and not pxe_network:
                pxe_network = tokens[0]
            continue
        match = _BOOT_RE.match(line)
        if match:
            boots.append(match.group(1).strip())
    return ranges, boots, pxe_network


def _dnsmasq_path() -> Path:
    path = settings.dnsmasq_config_path
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def infer_dhcp_mode(range_directives: list[str]) -> str | None:
    if not range_directives:
        return None
    for directive in range_directives:
        if ",proxy" in directive.replace(" ", "").lower():
            return "proxy"
    return "server"


def build_recommendation(configured: str | None, detected_offers: list[dict[str, Any]]) -> str | None:
    if not detected_offers and configured is None:
        return "No DHCP detected. Configure dnsmasq as a DHCP server with `setup.sh --dhcp-mode server` or run probe first."
    if detected_offers and configured == "server":
        return (
            "An existing DHCP server is on this network but dnsmasq is configured as a full DHCP server. "
            "Rerun `setup.sh --dhcp-mode proxy` to avoid DHCP conflicts."
        )
    if not detected_offers and configured == "proxy":
        return (
            "No DHCP server detected on the network but dnsmasq is configured as proxyDHCP. "
            "PXE clients will not get IPs. Rerun `setup.sh --dhcp-mode server`."
        )
    return None


def check_dhcp(db: Session) -> DhcpStatus:
    config_path = _dnsmasq_path()
    ranges, boots, pxe_network = parse_dnsmasq_conf(config_path)
    override_setting = db.get(AppSetting, DHCP_MODE_OVERRIDE_KEY)
    override_mode = None
    if override_setting and override_setting.value_json:
        override_mode = override_setting.value_json.get("mode")
    configured_mode = override_mode or settings.dhcp_mode or infer_dhcp_mode(ranges)

    host_ip_setting = db.get(AppSetting, HOST_IP_OVERRIDE_KEY)
    host_ip_override = None
    if host_ip_setting and host_ip_setting.value_json:
        host_ip_override = host_ip_setting.value_json.get("host_ip") or None
    effective_host_ip = host_ip_override or settings.pxe_host_ip

    setting = db.get(AppSetting, DHCP_PROBE_SETTING_KEY)
    detected_offers: list[dict[str, Any]] = []
    detected_at: str | None = None
    detection_error: str | None = None
    detection_running = False
    detection_source: str | None = None
    if setting and setting.value_json:
        payload = setting.value_json
        detected_offers = payload.get("offers") or []
        detected_at = payload.get("checked_at")
        detection_error = payload.get("error")
        detection_running = bool(payload.get("running"))
        detection_source = payload.get("source")

    return DhcpStatus(
        configured_mode=configured_mode,
        pxe_network=settings.pxe_network or pxe_network,
        host_ip=effective_host_ip,
        bootloader_directives=boots,
        dhcp_range_directives=ranges,
        config_path=str(config_path),
        config_found=config_path.exists(),
        detected_offers=detected_offers,
        detected_at=detected_at,
        detection_error=detection_error,
        detection_running=detection_running,
        detection_source=detection_source,
        sidecar_configured=bool(settings.dhcp_probe_url),
        recommendation=build_recommendation(configured_mode, detected_offers),
    )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def compute_setup_status(db: Session) -> SetupStatus:
    distros, images_total = check_distros(db)
    return SetupStatus(
        bootloaders=check_bootloaders(),
        distros=distros,
        profiles=check_profiles(db),
        dhcp=check_dhcp(db),
        images_total=images_total,
    )
