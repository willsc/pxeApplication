from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.bootstrap import init_database
from app.database import SessionLocal
from app.models import Image, OSType
from app.services.profiles import ensure_default_profile
from app.services.unattended import ensure_unattended_default_profile


UBUNTU_BASE_URL = "https://releases.ubuntu.com"
MICROSOFT_WINDOWS_11_DOWNLOAD_URL = "https://www.microsoft.com/en-us/software-download/windows11"
IPXE_BOOT_BASE_URL = "https://boot.ipxe.org"
WIMBOOT_URL = "https://github.com/ipxe/wimboot/releases/latest/download/wimboot"
UBUNTU_LTS_ALIASES = {
    "22": "22.04.5",
    "22.04": "22.04.5",
    "22.04.5": "22.04.5",
    "jammy": "22.04.5",
    "24": "24.04.4",
    "24.04": "24.04.4",
    "24.04.4": "24.04.4",
    "noble": "24.04.4",
    "26": "26.04",
    "26.04": "26.04",
    "resolute": "26.04",
}
UBUNTU_DESKTOP_PRESETS = ["22", "24", "26"]
UBUNTU_SERVER_PRESETS = ["22", "24", "26"]

# iPXE publishes per-arch builds. boot.ipxe.org has only `undionly.kpxe` at the
# root for legacy BIOS PXE; UEFI binaries live under arch-specific subdirs.
IPXE_BOOT_ASSETS: list[tuple[str, str]] = [
    # (relative URL on boot.ipxe.org, destination relative to tftproot)
    ("undionly.kpxe", "undionly.kpxe"),
    ("x86_64-efi/ipxe.efi", "ipxe.efi"),
    ("x86_64-efi/snponly.efi", "snponly.efi"),
    ("i386-efi/ipxe.efi", "ipxe32.efi"),
]


class MediaImportError(RuntimeError):
    pass


PROGRESS_PREFIX = "PXE_PROGRESS:"
PHASE_PREFIX = "PXE_PHASE:"


def info(message: str) -> None:
    print(message, file=sys.stderr)


def phase(message: str) -> None:
    """Announce a high-level phase (parsed by media_jobs to set current step)."""
    print(f"{PHASE_PREFIX} {message}", file=sys.stderr, flush=True)


def progress(percent: int, hint: str = "") -> None:
    """Emit a parseable progress line. percent is 0-100."""
    pct = max(0, min(100, int(percent)))
    suffix = f" {hint}" if hint else ""
    print(f"{PROGRESS_PREFIX} {pct}{suffix}", file=sys.stderr, flush=True)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-")
    return slug or "image"


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaImportError(f"Required tool not found: {name}. Install libarchive-tools/bsdtar.")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    phase(f"Downloading {destination.name}")
    info(f"Downloading {url}")
    last_pct = -1
    with urllib.request.urlopen(url) as response, destination.open("wb") as output:
        total = int(response.headers.get("content-length") or 0)
        downloaded = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded * 100 // total
                # Emit on every full percent step so the streaming reader can update.
                if pct != last_pct:
                    last_pct = pct
                    mib = downloaded // (1024 * 1024)
                    total_mib = total // (1024 * 1024)
                    progress(pct, f"{mib}/{total_mib} MiB · {destination.name}")
        if total:
            progress(100, f"{destination.name} done")
    return destination


def filename_from_url(url: str) -> str:
    name = Path(urlparse(url).path).name
    if not name:
        raise MediaImportError(f"Cannot infer filename from URL: {url}")
    return name


def verify_sha256(path: Path, expected: str | None) -> str:
    actual = sha256_file(path)
    if expected and actual.lower() != expected.lower():
        raise MediaImportError(f"SHA256 mismatch for {path}: expected {expected}, got {actual}")
    info(f"SHA256 {actual}  {path.name}")
    return actual


def ubuntu_iso_name(version: str, edition: str) -> str:
    version = resolve_ubuntu_version(version)
    if edition == "server":
        return f"ubuntu-{version}-live-server-amd64.iso"
    if edition == "desktop":
        return f"ubuntu-{version}-desktop-amd64.iso"
    raise MediaImportError("Ubuntu edition must be server or desktop")


def resolve_ubuntu_version(version: str) -> str:
    return UBUNTU_LTS_ALIASES.get(version.strip().lower(), version)


def ubuntu_sha256(version: str, iso_name: str, download_dir: Path) -> str | None:
    sums_url = f"{UBUNTU_BASE_URL}/{version}/SHA256SUMS"
    sums_path = download_dir / f"ubuntu-{version}-SHA256SUMS"
    try:
        download_file(sums_url, sums_path)
    except Exception as exc:  # noqa: BLE001 - network failures should become operator-facing warnings.
        info(f"Warning: could not download SHA256SUMS: {exc}")
        return None
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*") == iso_name:
            return parts[0]
    info(f"Warning: {iso_name} not found in SHA256SUMS")
    return None


def list_iso_members(iso_path: Path) -> list[str]:
    bsdtar = require_tool("bsdtar")
    result = subprocess.run(
        [bsdtar, "-tf", str(iso_path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def find_member(members: list[str], candidates: list[str]) -> str:
    normalized = {member.lower().lstrip("./"): member for member in members}
    for candidate in candidates:
        found = normalized.get(candidate.lower().lstrip("./"))
        if found:
            return found
    raise MediaImportError(f"None of these ISO paths were found: {', '.join(candidates)}")


def extract_members(iso_path: Path, members: list[str], destination: Path) -> None:
    bsdtar = require_tool("bsdtar")
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [bsdtar, "-xf", str(iso_path), "-C", str(destination), *members],
        check=True,
    )


def copy_extracted(extract_dir: Path, member: str, destination: Path) -> None:
    source = extract_dir / member.lstrip("./")
    if not source.exists():
        raise MediaImportError(f"Extracted member missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def upsert_image(image: Image, replace: bool) -> Image:
    init_database()
    with SessionLocal() as db:
        existing = db.scalar(select(Image).where(Image.name == image.name))
        if existing and not replace:
            raise MediaImportError(f"Image {image.name!r} already exists. Pass --replace to update it.")
        target = existing or image
        if existing:
            for field in (
                "os_type",
                "architecture",
                "kernel_path",
                "initrd_path",
                "repo_url",
                "bootloader_path",
                "wim_path",
                "bcd_path",
                "boot_sdi_path",
                "extra_kernel_args",
                "metadata_json",
            ):
                setattr(existing, field, getattr(image, field))
        else:
            db.add(target)
        try:
            db.flush()
            ensure_default_profile(db, target)
            ensure_unattended_default_profile(db)
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise MediaImportError(str(exc)) from exc
        db.refresh(target)
        return target


def import_ubuntu(args: argparse.Namespace) -> int:
    download_dir = Path(args.download_dir)
    tftproot = Path(args.tftproot)
    version = resolve_ubuntu_version(args.version)
    iso_name = filename_from_url(args.url) if args.url else ubuntu_iso_name(args.version, args.edition)
    url = args.url or f"{UBUNTU_BASE_URL}/{version}/{iso_name}"
    iso_path = Path(args.iso) if args.iso else download_dir / iso_name
    expected_sha = args.sha256

    if not iso_path.exists():
        if not expected_sha and not args.url:
            expected_sha = ubuntu_sha256(version, iso_name, download_dir)
        download_file(url, iso_path)
    actual_sha = verify_sha256(iso_path, expected_sha)

    phase("Inspecting ISO contents")
    members = list_iso_members(iso_path)
    kernel_member = find_member(members, ["casper/vmlinuz", "casper/hwe-vmlinuz"])
    initrd_member = find_member(
        members,
        ["casper/initrd", "casper/initrd.lz", "casper/initrd.gz", "casper/hwe-initrd"],
    )
    dest_prefix = Path(args.destination or f"ubuntu/{version}-{args.edition}")
    kernel_dest = dest_prefix / "casper" / Path(kernel_member).name
    initrd_dest = dest_prefix / "casper" / Path(initrd_member).name
    iso_dest = dest_prefix / iso_name

    phase("Extracting kernel + initrd")
    with tempfile.TemporaryDirectory(prefix="pxe-iso-") as temp:
        extract_dir = Path(temp)
        extract_members(iso_path, [kernel_member, initrd_member], extract_dir)
        copy_extracted(extract_dir, kernel_member, tftproot / kernel_dest)
        copy_extracted(extract_dir, initrd_member, tftproot / initrd_dest)
    phase("Publishing ISO to tftproot")
    # Make the ISO itself reachable over HTTP so the live kernel can mount
    # casper/filesystem.squashfs from it via `url=...`. Hard-link when possible
    # to avoid doubling disk usage; fall back to copy across filesystems.
    iso_full_dest = tftproot / iso_dest
    iso_full_dest.parent.mkdir(parents=True, exist_ok=True)
    if iso_full_dest.exists() or iso_full_dest.is_symlink():
        iso_full_dest.unlink()
    try:
        os.link(iso_path, iso_full_dest)
    except OSError:
        shutil.copy2(iso_path, iso_full_dest)

    image_name = args.name or f"ubuntu-{version}-{args.edition}"
    image = Image(
        name=image_name,
        os_type=OSType.UBUNTU,
        architecture="x86_64",
        kernel_path=str(kernel_dest),
        initrd_path=str(initrd_dest),
        # repo_url is reused as the network path to the served ISO. The boot
        # script appends it as `url=<repo_url>` so the live installer can mount
        # the squashfs from the served ISO rather than failing in tmpfs.
        repo_url=str(iso_dest),
        metadata_json={
            "source": "ubuntu-release",
            "edition": args.edition,
            "version": version,
            "requested_version": args.version,
            "iso_name": iso_name,
            "iso_url": url,
            "iso_sha256": actual_sha,
            "iso_served_path": str(iso_dest),
        },
    )
    saved = upsert_image(image, args.replace)
    print(f"Imported Ubuntu image {saved.name} with id={saved.id}")
    print(f"kernel_path={saved.kernel_path}")
    print(f"initrd_path={saved.initrd_path}")
    print(f"iso_path={iso_dest}")
    return 0


def import_ubuntu_desktops(args: argparse.Namespace) -> int:
    versions = args.versions or UBUNTU_DESKTOP_PRESETS
    for version in versions:
        child = argparse.Namespace(**vars(args))
        child.version = version
        child.edition = "desktop"
        child.iso = None
        child.url = None
        child.sha256 = None
        child.name = (
            args.name_prefix + resolve_ubuntu_version(version) + "-desktop"
            if args.name_prefix
            else None
        )
        child.destination = None
        import_ubuntu(child)
    return 0


def import_ubuntu_servers(args: argparse.Namespace) -> int:
    versions = args.versions or UBUNTU_SERVER_PRESETS
    for version in versions:
        child = argparse.Namespace(**vars(args))
        child.version = version
        child.edition = "server"
        child.iso = None
        child.url = None
        child.sha256 = None
        child.name = (
            args.name_prefix + resolve_ubuntu_version(version) + "-server"
            if args.name_prefix
            else None
        )
        child.destination = None
        import_ubuntu(child)
    return 0


def import_bootloaders(args: argparse.Namespace) -> int:
    tftproot = Path(args.tftproot)
    ipxe_base_url = args.ipxe_base_url.rstrip("/")
    assets: list[tuple[str, Path]] = [
        (f"{ipxe_base_url}/{rel_url}", tftproot / rel_dest)
        for rel_url, rel_dest in IPXE_BOOT_ASSETS
    ]
    if args.include_wimboot:
        # wimboot is referenced from tftproot/wimboot in install scripts; keep a
        # second copy under windows/ so any older Image rows still resolve.
        assets.append((args.wimboot_url, tftproot / "wimboot"))
        assets.append((args.wimboot_url, tftproot / "windows" / "wimboot"))

    manifest: dict[str, dict[str, str]] = {}
    for url, destination in assets:
        try:
            download_file(url, destination)
        except urllib.error.HTTPError as exc:
            raise MediaImportError(f"Failed to download {url}: {exc}") from exc
        digest = verify_sha256(destination, None)
        manifest[str(destination.relative_to(tftproot))] = {"url": url, "sha256": digest}

    manifest_path = tftproot / "bootloaders.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Downloaded bootloaders into {tftproot}")
    for path in manifest:
        print(path)
    return 0


def windows_download_help(_: argparse.Namespace) -> int:
    print("Windows 11 ISO download is facilitated through Microsoft's official temporary-link flow.")
    print()
    print(f"1. Open: {MICROSOFT_WINDOWS_11_DOWNLOAD_URL}")
    print("2. Use 'Download Windows 11 Disk Image (ISO) for x64 devices'.")
    print("3. Select Windows 11, choose the language, and copy the generated 24-hour ISO URL.")
    print("4. Import it with:")
    print("   scripts/prepare_media.sh --windows-url '<microsoft-temporary-iso-url>'")
    print()
    print("If you already downloaded the ISO:")
    print("   scripts/prepare_media.sh --windows-iso data/isos/Win11.iso")
    return 0


def import_windows(args: argparse.Namespace) -> int:
    if not args.iso and not args.url:
        raise MediaImportError("Windows import requires --iso or --url from the official Microsoft ISO flow")

    download_dir = Path(args.download_dir)
    tftproot = Path(args.tftproot)
    iso_path = Path(args.iso) if args.iso else download_dir / filename_from_url(args.url)
    if not iso_path.exists():
        download_file(args.url, iso_path)
    actual_sha = verify_sha256(iso_path, args.sha256)

    wimboot_path = Path(args.wimboot) if args.wimboot else None
    if not wimboot_path:
        for candidate in (tftproot / "wimboot", tftproot / "windows" / "wimboot"):
            if candidate.exists():
                wimboot_path = candidate
                break
    if not wimboot_path or not wimboot_path.exists():
        raise MediaImportError(
            "Windows PXE requires iPXE wimboot. Run the bootloader import first or pass "
            "--wimboot /path/to/wimboot. Expected location: tftproot/wimboot."
        )

    phase("Inspecting Windows ISO")
    members = list_iso_members(iso_path)
    boot_wim = find_member(members, ["sources/boot.wim", "x64/sources/boot.wim"])
    bcd = find_member(
        members,
        [
            "boot/bcd",
            "efi/microsoft/boot/bcd",
            "x64/boot/bcd",
            "x64/efi/microsoft/boot/bcd",
        ],
    )
    boot_sdi = find_member(
        members,
        [
            "boot/boot.sdi",
            "efi/microsoft/boot/boot.sdi",
            "x64/boot/boot.sdi",
            "x64/efi/microsoft/boot/boot.sdi",
        ],
    )

    image_name = args.name or "windows-11"
    dest_prefix = Path(args.destination or f"windows/{slugify(image_name)}")
    wimboot_dest = dest_prefix / "wimboot"
    bcd_dest = dest_prefix / "boot" / "BCD"
    boot_sdi_dest = dest_prefix / "boot" / "boot.sdi"
    boot_wim_dest = dest_prefix / "sources" / "boot.wim"

    phase("Extracting Windows boot files")
    with tempfile.TemporaryDirectory(prefix="pxe-iso-") as temp:
        extract_dir = Path(temp)
        extract_members(iso_path, [boot_wim, bcd, boot_sdi], extract_dir)
        copy_extracted(extract_dir, boot_wim, tftproot / boot_wim_dest)
        copy_extracted(extract_dir, bcd, tftproot / bcd_dest)
        copy_extracted(extract_dir, boot_sdi, tftproot / boot_sdi_dest)
    (tftproot / wimboot_dest).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(wimboot_path, tftproot / wimboot_dest)

    image = Image(
        name=image_name,
        os_type=OSType.WINDOWS,
        architecture="x86_64",
        bootloader_path=str(wimboot_dest),
        wim_path=str(boot_wim_dest),
        bcd_path=str(bcd_dest),
        boot_sdi_path=str(boot_sdi_dest),
        metadata_json={
            "source": "windows-iso",
            "iso_name": iso_path.name,
            "iso_url": args.url,
            "iso_sha256": actual_sha,
        },
    )
    saved = upsert_image(image, args.replace)
    print(f"Imported Windows image {saved.name} with id={saved.id}")
    print(f"bootloader_path={saved.bootloader_path}")
    print(f"wim_path={saved.wim_path}")
    return 0


# ---------------------------------------------------------------------------
# Debian netboot importer.
# ---------------------------------------------------------------------------

DEBIAN_BASE_URL = "http://ftp.debian.org/debian"
DEBIAN_NETBOOT_TEMPLATE = (
    "{base}/dists/{codename}/main/installer-amd64/current/images/netboot/netboot.tar.gz"
)
DEBIAN_CODENAMES = {
    "11": "bullseye",
    "bullseye": "bullseye",
    "12": "bookworm",
    "bookworm": "bookworm",
    "13": "trixie",
    "trixie": "trixie",
}


def resolve_debian_codename(version: str) -> str:
    return DEBIAN_CODENAMES.get(version.strip().lower(), version)


def import_debian(args: argparse.Namespace) -> int:
    codename = resolve_debian_codename(args.version)
    base_url = args.mirror.rstrip("/")
    netboot_url = args.url or DEBIAN_NETBOOT_TEMPLATE.format(base=base_url, codename=codename)
    download_dir = Path(args.download_dir)
    tftproot = Path(args.tftproot)
    tarball_path = download_dir / f"debian-{codename}-netboot.tar.gz"

    if not tarball_path.exists():
        download_file(netboot_url, tarball_path)
    actual_sha = verify_sha256(tarball_path, args.sha256)

    dest_prefix = Path(args.destination or f"debian/{codename}")
    kernel_member = "debian-installer/amd64/linux"
    initrd_member = "debian-installer/amd64/initrd.gz"
    with tempfile.TemporaryDirectory(prefix="pxe-deb-") as temp:
        extract_dir = Path(temp)
        subprocess.run(
            [require_tool("tar"), "-xzf", str(tarball_path), "-C", str(extract_dir),
             kernel_member, initrd_member],
            check=True,
        )
        copy_extracted(extract_dir, kernel_member, tftproot / dest_prefix / "linux")
        copy_extracted(extract_dir, initrd_member, tftproot / dest_prefix / "initrd.gz")

    image_name = args.name or f"debian-{codename}"
    image = Image(
        name=image_name,
        os_type=OSType.DEBIAN,
        architecture="x86_64",
        kernel_path=str(dest_prefix / "linux"),
        initrd_path=str(dest_prefix / "initrd.gz"),
        repo_url=f"{base_url}/",
        metadata_json={
            "source": "debian-netboot",
            "codename": codename,
            "requested_version": args.version,
            "netboot_url": netboot_url,
            "tarball_sha256": actual_sha,
        },
    )
    saved = upsert_image(image, args.replace)
    print(f"Imported Debian image {saved.name} with id={saved.id}")
    print(f"kernel_path={saved.kernel_path}")
    print(f"initrd_path={saved.initrd_path}")
    return 0


def import_debian_set(args: argparse.Namespace) -> int:
    versions = args.versions or ["trixie"]
    for version in versions:
        child = argparse.Namespace(**vars(args))
        child.version = version
        child.url = None
        child.sha256 = None
        child.name = None
        child.destination = None
        import_debian(child)
    return 0


# ---------------------------------------------------------------------------
# RHEL-family importers (Rocky, AlmaLinux, Fedora).
#
# These download `images/pxeboot/vmlinuz` and `images/pxeboot/initrd.img`
# directly from the upstream mirror — no ISO required. The mirror URL becomes
# `repo_url`, which the kickstart template uses as `inst.repo=`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RHELFamily:
    slug: str
    label: str
    repo_template: str
    default_versions: tuple[str, ...]
    name_template: str


_RHEL_FAMILIES = {
    "rocky": RHELFamily(
        slug="rocky",
        label="Rocky Linux",
        repo_template="https://download.rockylinux.org/pub/rocky/{version}/BaseOS/x86_64/os",
        default_versions=("9", "10"),
        name_template="rocky-{version}",
    ),
    "almalinux": RHELFamily(
        slug="almalinux",
        label="AlmaLinux",
        repo_template="https://repo.almalinux.org/almalinux/{version}/BaseOS/x86_64/os",
        default_versions=("9", "10"),
        name_template="almalinux-{version}",
    ),
    "fedora": RHELFamily(
        slug="fedora",
        label="Fedora",
        repo_template="https://dl.fedoraproject.org/pub/fedora/linux/releases/{version}/Everything/x86_64/os",
        default_versions=("43",),
        name_template="fedora-{version}",
    ),
}


def _import_rhel_family(family: RHELFamily, args: argparse.Namespace) -> int:
    version = args.version
    repo_base = (args.repo_url or family.repo_template.format(version=version)).rstrip("/")
    kernel_url = f"{repo_base}/images/pxeboot/vmlinuz"
    initrd_url = f"{repo_base}/images/pxeboot/initrd.img"

    tftproot = Path(args.tftproot)
    dest_prefix = Path(args.destination or f"{family.slug}/{version}")
    kernel_dest = tftproot / dest_prefix / "vmlinuz"
    initrd_dest = tftproot / dest_prefix / "initrd.img"

    download_file(kernel_url, kernel_dest)
    download_file(initrd_url, initrd_dest)
    kernel_sha = verify_sha256(kernel_dest, None)
    initrd_sha = verify_sha256(initrd_dest, None)

    image_name = args.name or family.name_template.format(version=version)
    image = Image(
        name=image_name,
        os_type=OSType.RHEL,
        architecture="x86_64",
        kernel_path=str(dest_prefix / "vmlinuz"),
        initrd_path=str(dest_prefix / "initrd.img"),
        repo_url=repo_base + "/",
        metadata_json={
            "source": f"{family.slug}-mirror",
            "label": family.label,
            "version": version,
            "kernel_url": kernel_url,
            "initrd_url": initrd_url,
            "kernel_sha256": kernel_sha,
            "initrd_sha256": initrd_sha,
        },
    )
    saved = upsert_image(image, args.replace)
    print(f"Imported {family.label} image {saved.name} with id={saved.id}")
    print(f"repo_url={saved.repo_url}")
    print(f"kernel_path={saved.kernel_path}")
    print(f"initrd_path={saved.initrd_path}")
    return 0


def _import_rhel_set(family: RHELFamily, args: argparse.Namespace) -> int:
    versions = args.versions or list(family.default_versions)
    for version in versions:
        child = argparse.Namespace(**vars(args))
        child.version = version
        child.repo_url = None
        child.name = None
        child.destination = None
        _import_rhel_family(family, child)
    return 0


def import_rocky(args: argparse.Namespace) -> int:
    return _import_rhel_family(_RHEL_FAMILIES["rocky"], args)


def import_rocky_set(args: argparse.Namespace) -> int:
    return _import_rhel_set(_RHEL_FAMILIES["rocky"], args)


def import_almalinux(args: argparse.Namespace) -> int:
    return _import_rhel_family(_RHEL_FAMILIES["almalinux"], args)


def import_almalinux_set(args: argparse.Namespace) -> int:
    return _import_rhel_set(_RHEL_FAMILIES["almalinux"], args)


def import_fedora(args: argparse.Namespace) -> int:
    return _import_rhel_family(_RHEL_FAMILIES["fedora"], args)


def import_fedora_set(args: argparse.Namespace) -> int:
    return _import_rhel_set(_RHEL_FAMILIES["fedora"], args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download/import OS install media into pxe-app")
    parser.add_argument("--tftproot", default="tftproot", help="PXE file root. Default: tftproot")
    parser.add_argument("--download-dir", default="data/downloads", help="ISO download cache. Default: data/downloads")
    parser.add_argument("--replace", action="store_true", help="Update an existing Image with the same name")
    sub = parser.add_subparsers(dest="command", required=True)

    ubuntu = sub.add_parser("ubuntu", help="Download/import Ubuntu Desktop or Server ISO")
    ubuntu.add_argument(
        "--version",
        default="26",
        help="Ubuntu release alias/version. Supports 22, 24, 26, 22.04.5, 24.04.4, 26.04. Default: 26.",
    )
    ubuntu.add_argument("--edition", choices=["desktop", "server"], required=True)
    ubuntu.add_argument("--iso", help="Use an existing local ISO instead of downloading")
    ubuntu.add_argument("--url", help="Override ISO URL")
    ubuntu.add_argument("--sha256", help="Expected ISO SHA256")
    ubuntu.add_argument("--name", help="Image name to create")
    ubuntu.add_argument("--destination", help="Relative destination under tftproot")
    ubuntu.set_defaults(func=import_ubuntu)

    ubuntu_desktops = sub.add_parser("ubuntu-desktops", help="Import Ubuntu 22, 24, and 26 Desktop ISOs")
    ubuntu_desktops.add_argument(
        "--versions",
        nargs="+",
        default=UBUNTU_DESKTOP_PRESETS,
        help="Ubuntu desktop aliases/versions to import. Default: 22 24 26",
    )
    ubuntu_desktops.add_argument("--name-prefix", default="", help="Optional prefix for generated Image names")
    ubuntu_desktops.set_defaults(func=import_ubuntu_desktops)

    ubuntu_servers = sub.add_parser("ubuntu-servers", help="Import Ubuntu 22, 24, and 26 Server ISOs")
    ubuntu_servers.add_argument(
        "--versions",
        nargs="+",
        default=UBUNTU_SERVER_PRESETS,
        help="Ubuntu server aliases/versions to import. Default: 22 24 26",
    )
    ubuntu_servers.add_argument("--name-prefix", default="", help="Optional prefix for generated Image names")
    ubuntu_servers.set_defaults(func=import_ubuntu_servers)

    bootloaders = sub.add_parser("bootloaders", help="Download iPXE chainload binaries into tftproot")
    bootloaders.add_argument("--ipxe-base-url", default=IPXE_BOOT_BASE_URL)
    bootloaders.add_argument("--wimboot-url", default=WIMBOOT_URL)
    bootloaders.add_argument("--no-wimboot", action="store_false", dest="include_wimboot")
    bootloaders.set_defaults(func=import_bootloaders, include_wimboot=True)

    windows_help = sub.add_parser("windows-download-help", help="Show the official Windows 11 ISO download flow")
    windows_help.set_defaults(func=windows_download_help)

    windows = sub.add_parser("windows", help="Import Windows 11 ISO from local file or official ISO URL")
    windows.add_argument("--iso", help="Existing local Windows ISO")
    windows.add_argument("--url", help="Official Microsoft ISO URL")
    windows.add_argument("--sha256", help="Expected ISO SHA256")
    windows.add_argument("--wimboot", help="Path to iPXE wimboot binary")
    windows.add_argument("--name", default="windows-11", help="Image name to create. Default: windows-11")
    windows.add_argument("--destination", help="Relative destination under tftproot")
    windows.set_defaults(func=import_windows)

    # Debian netboot
    debian = sub.add_parser("debian", help="Download/import Debian netboot installer")
    debian.add_argument("--version", default="trixie", help="Debian codename or major version. Default: trixie")
    debian.add_argument("--mirror", default=DEBIAN_BASE_URL, help="Debian mirror base URL")
    debian.add_argument("--url", help="Override netboot tarball URL")
    debian.add_argument("--sha256", help="Expected SHA256 of the netboot tarball")
    debian.add_argument("--name", help="Image name to create")
    debian.add_argument("--destination", help="Relative destination under tftproot")
    debian.set_defaults(func=import_debian)

    debian_set = sub.add_parser("debian-set", help="Import a set of Debian releases")
    debian_set.add_argument("--versions", nargs="+", default=["trixie"], help="Codenames or versions to import")
    debian_set.add_argument("--mirror", default=DEBIAN_BASE_URL)
    debian_set.set_defaults(func=import_debian_set)

    # RHEL family - Rocky
    rocky = sub.add_parser("rocky", help="Download/import Rocky Linux netboot kernel + initrd")
    rocky.add_argument("--version", default="10", help="Rocky major version (8, 9, 10). Default: 10")
    rocky.add_argument("--repo-url", help="Override mirror base URL (without /images/pxeboot)")
    rocky.add_argument("--name", help="Image name to create")
    rocky.add_argument("--destination", help="Relative destination under tftproot")
    rocky.set_defaults(func=import_rocky)

    rocky_set = sub.add_parser("rocky-set", help="Import multiple Rocky Linux releases")
    rocky_set.add_argument("--versions", nargs="+", default=list(_RHEL_FAMILIES["rocky"].default_versions))
    rocky_set.set_defaults(func=import_rocky_set)

    # RHEL family - AlmaLinux
    alma = sub.add_parser("almalinux", help="Download/import AlmaLinux netboot kernel + initrd")
    alma.add_argument("--version", default="10", help="AlmaLinux major version (8, 9, 10). Default: 10")
    alma.add_argument("--repo-url", help="Override mirror base URL")
    alma.add_argument("--name", help="Image name to create")
    alma.add_argument("--destination", help="Relative destination under tftproot")
    alma.set_defaults(func=import_almalinux)

    alma_set = sub.add_parser("almalinux-set", help="Import multiple AlmaLinux releases")
    alma_set.add_argument("--versions", nargs="+", default=list(_RHEL_FAMILIES["almalinux"].default_versions))
    alma_set.set_defaults(func=import_almalinux_set)

    # RHEL family - Fedora
    fedora = sub.add_parser("fedora", help="Download/import Fedora netboot kernel + initrd")
    fedora.add_argument("--version", default="43", help="Fedora release. Default: 43")
    fedora.add_argument("--repo-url", help="Override mirror base URL")
    fedora.add_argument("--name", help="Image name to create")
    fedora.add_argument("--destination", help="Relative destination under tftproot")
    fedora.set_defaults(func=import_fedora)

    fedora_set = sub.add_parser("fedora-set", help="Import multiple Fedora releases")
    fedora_set.add_argument("--versions", nargs="+", default=list(_RHEL_FAMILIES["fedora"].default_versions))
    fedora_set.set_defaults(func=import_fedora_set)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (MediaImportError, subprocess.CalledProcessError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
