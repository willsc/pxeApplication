from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlencode

from app.models import Host, OSType
from app.settings import Settings


MAC_RE = re.compile(r"^[0-9a-f]{12}$")


def normalize_mac(value: str) -> str:
    compact = re.sub(r"[^0-9A-Fa-f]", "", value or "").lower()
    if not MAC_RE.match(compact):
        raise ValueError(f"Invalid MAC address: {value!r}")
    return ":".join(compact[i : i + 2] for i in range(0, 12, 2))


def token_prefix(token: str | None) -> str | None:
    return token[:8] if token else None


@dataclass(frozen=True)
class RenderedBootScript:
    body: str
    status_code: int = 200


def _script(lines: list[str]) -> RenderedBootScript:
    return RenderedBootScript("\n".join(["#!ipxe", *lines, ""]) )


def bootstrap_script(settings: Settings) -> RenderedBootScript:
    target = f"{settings.public_base_url}/boot.ipxe?mac=${{net0/mac}}&uuid=${{uuid}}&asset=${{asset}}"
    return _script(
        [
            "set 210:string pxeboot",
            f"chain --autofree {target} || goto failed",
            ":failed",
            "echo Failed to chainload pxe-app boot script",
            "sleep 5",
            "exit",
        ]
    )


def localboot_script(message: str = "No network install is pending for this host.") -> RenderedBootScript:
    return _script(
        [
            f"echo {message}",
            "sleep 2",
            "exit",
        ]
    )


def unknown_menu_script(mac: str, settings: Settings) -> RenderedBootScript:
    query = urlencode({"mac": mac})
    register_url = f"{settings.public_base_url}/api/boot/register?{query}"
    return _script(
        [
            f"echo Unknown host {mac}",
            "menu PXE provisioning",
            "item --key r register Register this host and boot from disk",
            "item --key l local Boot from local disk",
            "item --key n retry Retry network boot",
            "choose --timeout 15000 --default local selected || goto local",
            "goto ${selected}",
            ":register",
            f"chain --autofree {register_url} || goto local",
            "goto local",
            ":retry",
            "reboot",
            ":local",
            "exit",
        ]
    )


def register_then_localboot_script(mac: str, settings: Settings) -> RenderedBootScript:
    query = urlencode({"mac": mac})
    register_url = f"{settings.public_base_url}/api/boot/register?{query}"
    return _script(
        [
            f"echo Registering unknown host {mac}",
            f"chain --autofree {register_url} || goto local",
            ":local",
            "exit",
        ]
    )


def registered_script(host: Host) -> RenderedBootScript:
    return localboot_script(f"Registered {host.mac}; assign a profile in pxe-app before installing.")


def install_script(host: Host, settings: Settings) -> RenderedBootScript:
    if not host.profile or not host.profile.image:
        return localboot_script("Host has no install profile assigned.")

    image = host.profile.image
    config_url = f"{settings.public_base_url}/api/boot/config/{host.install_token}"
    files = settings.files_base_url
    extra = image.extra_kernel_args or ""

    if image.os_type == OSType.RHEL:
        if not image.kernel_path or not image.initrd_path:
            return localboot_script("RHEL image is missing kernel_path or initrd_path.")
        args = [
            "ip=dhcp",
            f"inst.ks={config_url}",
        ]
        if image.repo_url:
            args.append(f"inst.repo={image.repo_url}")
        if extra:
            args.append(extra)
        return _script(
            [
                f"kernel {files}/{image.kernel_path.lstrip('/')} {' '.join(args)}",
                f"initrd {files}/{image.initrd_path.lstrip('/')}",
                "boot",
            ]
        )

    if image.os_type == OSType.UBUNTU:
        if not image.kernel_path or not image.initrd_path:
            return localboot_script("Ubuntu image is missing kernel_path or initrd_path.")
        seed_url = f"{settings.public_base_url}/api/boot/seed/{host.install_token}/"
        args = ["ip=dhcp", "ip6=off", "autoinstall", f"ds=nocloud-net;s={seed_url}"]
        # repo_url is set by the importer to the relative path of the served ISO
        # under tftproot. The live kernel needs `url=<iso-url>` to mount
        # casper/filesystem.squashfs over HTTP. If repo_url is an absolute URL
        # (operator-set), use it as-is.
        if image.repo_url:
            iso_url = image.repo_url
            if not iso_url.startswith(("http://", "https://")):
                iso_url = f"{files}/{iso_url.lstrip('/')}"
            args.append(f"url={iso_url}")
        if extra:
            args.append(extra)
        return _script(
            [
                f"kernel {files}/{image.kernel_path.lstrip('/')} {' '.join(args)}",
                f"initrd {files}/{image.initrd_path.lstrip('/')}",
                "boot",
            ]
        )

    if image.os_type == OSType.DEBIAN:
        if not image.kernel_path or not image.initrd_path:
            return localboot_script("Debian image is missing kernel_path or initrd_path.")
        args = [
            "auto=true",
            "priority=critical",
            f"url={config_url}",
            "interface=auto",
            "netcfg/dhcp_timeout=60",
        ]
        if extra:
            args.append(extra)
        return _script(
            [
                f"kernel {files}/{image.kernel_path.lstrip('/')} {' '.join(args)}",
                f"initrd {files}/{image.initrd_path.lstrip('/')}",
                "boot",
            ]
        )

    if image.os_type == OSType.WINDOWS:
        missing = [
            name
            for name, value in {
                "bootloader_path": image.bootloader_path,
                "bcd_path": image.bcd_path,
                "boot_sdi_path": image.boot_sdi_path,
                "wim_path": image.wim_path,
            }.items()
            if not value
        ]
        if missing:
            return localboot_script(f"Windows image is missing {', '.join(missing)}.")
        return _script(
            [
                f"kernel {files}/{image.bootloader_path.lstrip('/')}",
                f"initrd {files}/{image.bcd_path.lstrip('/')} BCD",
                f"initrd {files}/{image.boot_sdi_path.lstrip('/')} boot.sdi",
                f"initrd {files}/{image.wim_path.lstrip('/')} boot.wim",
                f"initrd {config_url} Autounattend.xml",
                "boot",
            ]
        )

    return localboot_script("Unsupported OS type.")
