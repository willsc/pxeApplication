"""Run a DHCP probe and persist the result.

The probe needs to see DHCP OFFER packets on the operator's PXE VLAN, which
means binding UDP/68 with CAP_NET_BIND_SERVICE. The default docker-compose
setup runs pxe-app on a bridge network with no capabilities, so a direct
subprocess probe inside pxe-app cannot see the host's network.

To make probing actually work we call out to a sidecar HTTP service
(``app.dhcp_probe_server``) that runs in a container with
``network_mode: host`` and the required capabilities. The address is taken
from ``PXE_DHCP_PROBE_URL`` (or ``settings.dhcp_probe_url``).

If the sidecar isn't reachable we fall back to running the probe in-process
via ``app.dhcp_detect`` so the codepath still works for tests and CLI use.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import timezone
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import AppSetting, utcnow
from app.services.setup_status import DHCP_PROBE_SETTING_KEY
from app.settings import settings


PROBE_TIMEOUT_SECONDS = 30
PROBE_RUNNING_FLAG = "running"


def _store(db: Session, payload: dict[str, Any]) -> None:
    setting = db.get(AppSetting, DHCP_PROBE_SETTING_KEY)
    if not setting:
        setting = AppSetting(key=DHCP_PROBE_SETTING_KEY, value_json=payload)
        db.add(setting)
    else:
        setting.value_json = payload
    db.commit()


def mark_probe_starting() -> None:
    """Record a 'running' placeholder so the UI shows progress immediately."""
    checked_at = utcnow().astimezone(timezone.utc).isoformat()
    with SessionLocal() as db:
        existing = db.get(AppSetting, DHCP_PROBE_SETTING_KEY)
        previous: dict[str, Any] = (existing.value_json if existing else {}) or {}
        payload = {
            "offers": previous.get("offers") or [],
            "checked_at": previous.get("checked_at"),
            PROBE_RUNNING_FLAG: True,
            "started_at": checked_at,
        }
        _store(db, payload)


def _probe_via_sidecar(url: str) -> dict[str, Any]:
    body = json.dumps(
        {
            "interface": settings.pxe_probe_interface,
            "timeout": settings.pxe_probe_timeout,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
        raw = response.read().decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Probe sidecar returned non-object JSON")
    parsed.setdefault("offers", [])
    return parsed


def _probe_via_subprocess() -> dict[str, Any]:
    command = [sys.executable, "-m", "app.dhcp_detect", "--json"]
    if settings.pxe_probe_interface:
        command.extend(["--interface", settings.pxe_probe_interface])
    if settings.pxe_probe_timeout:
        command.extend(["--timeout", str(settings.pxe_probe_timeout)])

    payload: dict[str, Any] = {"command": command}
    try:
        result = subprocess.run(
            command,
            cwd=os.getcwd(),
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode == 0 and result.stdout:
            try:
                parsed = json.loads(result.stdout)
                payload["offers"] = parsed.get("offers") or []
            except json.JSONDecodeError:
                payload["offers"] = []
                payload["error"] = f"Invalid probe output: {result.stdout[:200]}"
        elif result.returncode == 1:
            payload["offers"] = []
        elif result.returncode == 3:
            payload["offers"] = []
            payload["error"] = (
                "DHCP probe requires the pxe-probe sidecar (network_mode: host "
                "+ NET_BIND_SERVICE). Set PXE_DHCP_PROBE_URL or run "
                "`scripts/check_dhcp.sh` on the host."
            )
        else:
            payload["offers"] = []
            payload["error"] = (result.stderr or result.stdout or "probe failed").strip()
    except subprocess.TimeoutExpired:
        payload["offers"] = []
        payload["error"] = f"Probe timed out after {PROBE_TIMEOUT_SECONDS} seconds"
    except FileNotFoundError as exc:
        payload["offers"] = []
        payload["error"] = f"Probe binary missing: {exc}"
    return payload


def run_dhcp_probe() -> None:
    """Run a single DHCP probe and persist the latest result."""
    checked_at = utcnow().astimezone(timezone.utc).isoformat()
    payload: dict[str, Any] = {"checked_at": checked_at}
    sidecar_url = settings.dhcp_probe_url
    try:
        if sidecar_url:
            try:
                result = _probe_via_sidecar(sidecar_url)
                payload.update(result)
                payload["source"] = "sidecar"
                payload["checked_at"] = result.get("checked_at") or checked_at
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                payload = {
                    "checked_at": checked_at,
                    "offers": [],
                    "error": (
                        f"Probe sidecar at {sidecar_url} unreachable: {exc}. "
                        "Is the pxe-probe service running?"
                    ),
                    "source": "sidecar-unreachable",
                }
            except (ValueError, json.JSONDecodeError) as exc:
                payload = {
                    "checked_at": checked_at,
                    "offers": [],
                    "error": f"Probe sidecar returned invalid response: {exc}",
                    "source": "sidecar-bad-response",
                }
        else:
            sub_result = _probe_via_subprocess()
            payload.update(sub_result)
            payload["source"] = "in-process"
    finally:
        payload[PROBE_RUNNING_FLAG] = False
        with SessionLocal() as db:
            _store(db, payload)
