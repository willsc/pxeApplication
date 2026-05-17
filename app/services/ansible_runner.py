from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import AnsibleRun, AnsibleRunState, Host, PostInstallConfig, utcnow
from app.settings import settings


class AnsibleRunError(RuntimeError):
    pass


def _safe_relative_path(value: str, base_dir: Path) -> Path:
    requested = Path(value)
    if requested.is_absolute() or ".." in requested.parts:
        raise AnsibleRunError("Ansible playbook path must be relative to ansible/playbooks")
    base = base_dir.resolve()
    resolved = (base / requested).resolve()
    if base not in resolved.parents and resolved != base:
        raise AnsibleRunError("Ansible playbook path escapes ansible/playbooks")
    return resolved


def _quote_inventory_value(value: Any) -> str:
    if isinstance(value, bool):
        value = "true" if value else "false"
    return shlex.quote(str(value))


def _host_target(host: Host) -> str:
    variables = host.variables or {}
    target = variables.get("ansible_host") or variables.get("ip_address") or host.hostname
    if not target:
        raise AnsibleRunError(
            "Host needs hostname, variables.ansible_host, or variables.ip_address for Ansible"
        )
    return str(target)


def _inventory_hostname(host: Host) -> str:
    return (host.hostname or f"host-{host.mac.replace(':', '')}").replace(" ", "-")


def _inventory_groups(config: PostInstallConfig) -> list[str]:
    raw = config.inventory_groups or "pxe_app"
    groups = [group.strip() for group in raw.replace(",", "\n").splitlines() if group.strip()]
    return groups or ["pxe_app"]


def render_inventory(host: Host, config: PostInstallConfig) -> str:
    host_vars: dict[str, Any] = {
        "ansible_host": _host_target(host),
        "ansible_user": config.ssh_user,
        "ansible_port": config.ssh_port,
    }
    if config.ssh_private_key_path:
        host_vars["ansible_ssh_private_key_file"] = config.ssh_private_key_path
    host_vars.update(config.inventory_vars or {})
    ansible_vars = (host.variables or {}).get("ansible")
    if isinstance(ansible_vars, dict):
        host_vars.update(ansible_vars)

    hostname = _inventory_hostname(host)
    host_line = " ".join(
        [hostname, *[f"{key}={_quote_inventory_value(value)}" for key, value in host_vars.items()]]
    )
    sections = [f"[{group}]\n{host_line}" for group in _inventory_groups(config)]
    return "\n\n".join(sections) + "\n"


def build_command(run: AnsibleRun, config: PostInstallConfig, playbook: Path) -> list[str]:
    executable = shutil.which("ansible-playbook") or "ansible-playbook"
    command = [
        executable,
        "-i",
        str(run.inventory_path),
        str(playbook),
        "--limit",
        _inventory_hostname(run.host),
    ]
    if config.become:
        command.append("--become")
    if config.extra_vars:
        command.extend(["--extra-vars", json.dumps(config.extra_vars)])
    if config.tags:
        command.extend(["--tags", config.tags])
    if config.skip_tags:
        command.extend(["--skip-tags", config.skip_tags])
    return command


def build_environment(run: AnsibleRun, config: PostInstallConfig) -> dict[str, str]:
    env = os.environ.copy()
    run_dir = settings.ansible_work_dir / str(run.id)
    home_dir = run_dir / "home"
    local_tmp = run_dir / "tmp"
    home_dir.mkdir(parents=True, exist_ok=True)
    local_tmp.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home_dir)
    env["ANSIBLE_LOCAL_TEMP"] = str(local_tmp)
    env["ANSIBLE_HOST_KEY_CHECKING"] = "True" if config.host_key_checking else "False"
    return env


def queue_ansible_run(
    db: Session,
    host: Host,
    *,
    trigger: str = "callback",
    build_record_id: int | None = None,
) -> AnsibleRun | None:
    if not host.profile or not host.profile.post_install_config:
        return None
    config = host.profile.post_install_config
    if not config.enabled:
        return None
    run = AnsibleRun(
        host=host,
        profile_id=host.profile_id,
        config=config,
        build_record_id=build_record_id,
        state=AnsibleRunState.QUEUED,
        playbook_path=config.playbook_path,
        trigger=trigger,
        metadata_json={
            "host_mac": host.mac,
            "hostname": host.hostname,
            "profile": host.profile.name,
        },
    )
    db.add(run)
    db.flush()
    return run


def run_ansible_run(run_id: int) -> None:
    with SessionLocal() as db:
        run = db.get(AnsibleRun, run_id)
        if not run:
            return
        if run.state != AnsibleRunState.QUEUED:
            return
        config = run.config
        host = run.host
        if not config or not host:
            run.state = AnsibleRunState.SKIPPED
            run.error = "Ansible config or host was deleted before run started"
            run.completed_at = utcnow()
            db.commit()
            return

        try:
            playbook = _safe_relative_path(config.playbook_path, settings.ansible_playbooks_dir)
            if not playbook.exists():
                raise AnsibleRunError(f"Playbook does not exist: {playbook}")

            work_dir = settings.ansible_work_dir / str(run.id)
            work_dir.mkdir(parents=True, exist_ok=True)
            inventory_path = work_dir / "inventory.ini"
            inventory_path.write_text(render_inventory(host, config), encoding="utf-8")
            run.inventory_path = str(inventory_path)
            run.command = build_command(run, config, playbook)
            run.state = AnsibleRunState.RUNNING
            run.started_at = utcnow()
            db.commit()

            result = subprocess.run(
                run.command,
                cwd=str(settings.ansible_playbooks_dir),
                env=build_environment(run, config),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=config.timeout_seconds,
                check=False,
            )
            run.return_code = result.returncode
            run.stdout = result.stdout[-128_000:]
            run.stderr = result.stderr[-128_000:]
            run.state = AnsibleRunState.SUCCESS if result.returncode == 0 else AnsibleRunState.FAILED
            run.completed_at = utcnow()
            db.commit()
        except subprocess.TimeoutExpired as exc:
            run.return_code = None
            run.stdout = (exc.stdout or "")[-128_000:] if isinstance(exc.stdout, str) else None
            run.stderr = (exc.stderr or "")[-128_000:] if isinstance(exc.stderr, str) else None
            run.error = f"Timed out after {config.timeout_seconds} seconds"
            run.state = AnsibleRunState.TIMEOUT
            run.completed_at = utcnow()
            db.commit()
        except Exception as exc:  # noqa: BLE001 - persisted for operator diagnostics.
            run.error = str(exc)
            run.state = AnsibleRunState.FAILED
            run.completed_at = utcnow()
            db.commit()


def retry_ansible_run(db: Session, previous: AnsibleRun) -> AnsibleRun:
    host = previous.host
    if not host:
        raise AnsibleRunError("Original host no longer exists")
    run = queue_ansible_run(db, host, trigger="manual-retry", build_record_id=previous.build_record_id)
    if not run:
        raise AnsibleRunError("Host profile has no enabled Ansible post-install config")
    return run


def latest_runs_for_host(db: Session, host_id: int, limit: int = 20) -> list[AnsibleRun]:
    return db.scalars(
        select(AnsibleRun)
        .where(AnsibleRun.host_id == host_id)
        .order_by(AnsibleRun.queued_at.desc(), AnsibleRun.id.desc())
        .limit(limit)
    ).all()
