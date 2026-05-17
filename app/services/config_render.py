from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound
from passlib.hash import sha512_crypt

from app.models import Host, OSType
from app.security import generate_random_token
from app.settings import Settings


DEFAULT_TEMPLATES = {
    OSType.RHEL: "kickstart/rhel.ks.j2",
    OSType.UBUNTU: "autoinstall/ubuntu.yaml.j2",
    OSType.DEBIAN: "preseed/debian.cfg.j2",
    OSType.WINDOWS: "windows/autounattend.xml.j2",
}


class ConfigRenderError(RuntimeError):
    pass


def _safe_template_path(template_path: str) -> str:
    path = Path(template_path)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigRenderError("Template path must be relative and stay inside pxe_templates")
    return str(path)


def _encode_powershell(script: str) -> str:
    """Base64-encode UTF-16LE so Windows can run scripts via -EncodedCommand.

    PowerShell's -EncodedCommand expects a UTF-16LE BOM-less base64 blob.
    This sidesteps every escaping headache that XML+CMD+PS chained together
    would otherwise cause.
    """
    return base64.b64encode((script or "").encode("utf-16-le")).decode("ascii")


def _environment(settings: Settings) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(settings.pxe_templates_dir)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["encode_powershell"] = _encode_powershell
    return env


def authorized_keys(profile_keys: str | None) -> list[str]:
    if not profile_keys:
        return []
    return [line.strip() for line in profile_keys.splitlines() if line.strip()]


def parse_packages(raw: str | None) -> list[str]:
    """Split a freeform extra-packages blob into clean package names.

    Accepts whitespace- or comma-separated lists with optional ``#`` comments.
    Filters duplicates while preserving order so generated configs are stable.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for line in raw.splitlines():
        token = line.split("#", 1)[0]
        for chunk in token.replace(",", " ").split():
            name = chunk.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
    return out


def extras_for_host(host: Host) -> dict[str, Any]:
    """Pull the inline-finish-script extras for the host's profile."""
    extras = host.profile.extras if host.profile else None
    return {
        "packages": parse_packages(extras.extra_packages if extras else None),
        "bash": (extras.finish_script_bash if extras else None) or "",
        "powershell": (extras.finish_script_powershell if extras else None) or "",
        "ansible_pull_url": (extras.ansible_pull_url if extras else None) or "",
        "ansible_pull_playbook": (extras.ansible_pull_playbook if extras else None) or "",
    }


def merged_variables(host: Host) -> dict[str, Any]:
    profile_variables = host.profile.variables if host.profile else {}
    merged = dict(profile_variables or {})
    merged.update(host.variables or {})
    return merged


def media_type_for_template(template_path: str) -> str:
    suffix = Path(template_path).suffix.lower()
    if suffix == ".xml":
        return "application/xml"
    if suffix in {".yaml", ".yml"}:
        return "text/yaml"
    return "text/plain"


def template_for_host(host: Host) -> str:
    if not host.profile or not host.profile.image:
        raise ConfigRenderError("Host has no profile/image")
    template_path = host.profile.template_path or DEFAULT_TEMPLATES[host.profile.image.os_type]
    return _safe_template_path(template_path)


def render_config(host: Host, settings: Settings) -> tuple[str, str]:
    if not host.profile or not host.profile.image:
        raise ConfigRenderError("Host has no profile/image")

    template_path = template_for_host(host)
    env = _environment(settings)
    try:
        template = env.get_template(template_path)
    except TemplateNotFound as exc:
        raise ConfigRenderError(f"Template not found: {template_path}") from exc

    root_password = host.profile.root_password
    root_password_hash = sha512_crypt.hash(root_password) if root_password else None
    context = {
        "image": host.profile.image,
        "profile": host.profile,
        "host": {
            "id": host.id,
            "mac": host.mac,
            "hostname": host.hostname,
            "state": host.state.value,
            "install_token": host.install_token,
            "config_url": f"{settings.public_base_url}/api/boot/config/{host.install_token}",
            "callback_url": f"{settings.public_base_url}/api/boot/callback/{host.install_token}",
            "variables": host.variables or {},
        },
        "vars": merged_variables(host),
        "authorized_keys": authorized_keys(host.profile.authorized_keys),
        "root_password": root_password,
        "root_password_hash": root_password_hash,
        "random_token": generate_random_token(16),
        "extras": extras_for_host(host),
    }
    return template.render(**context), media_type_for_template(template_path)

