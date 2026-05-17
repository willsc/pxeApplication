from __future__ import annotations

import io
import os
import re
import selectors
import subprocess
import sys
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import MediaImportRun, MediaImportState, utcnow
from app.services.profiles import ensure_default_profiles
from app.services.unattended import ensure_unattended_default_profile


OUTPUT_LIMIT = 128_000
IMPORT_TIMEOUT_SECONDS = 24 * 60 * 60


class MediaJobError(RuntimeError):
    pass


def _base_command(replace: bool = True) -> list[str]:
    command = [sys.executable, "-m", "app.media_import"]
    if replace:
        command.append("--replace")
    return command


def bootloaders_command() -> list[str]:
    return [*_base_command(), "bootloaders"]


def ubuntu_command(
    *,
    edition: str,
    version: str,
    name: str | None = None,
    url: str | None = None,
    sha256: str | None = None,
) -> list[str]:
    if edition not in {"desktop", "server"}:
        raise MediaJobError("Ubuntu edition must be desktop or server")
    command = [*_base_command(), "ubuntu", "--edition", edition, "--version", version]
    if name:
        command.extend(["--name", name])
    if url:
        command.extend(["--url", url])
    if sha256:
        command.extend(["--sha256", sha256])
    return command


def ubuntu_set_command(*, edition: str, versions: list[str]) -> list[str]:
    if edition == "desktop":
        subcommand = "ubuntu-desktops"
    elif edition == "server":
        subcommand = "ubuntu-servers"
    else:
        raise MediaJobError("Ubuntu set edition must be desktop or server")
    command = [*_base_command(), subcommand]
    if versions:
        command.extend(["--versions", *versions])
    return command


def windows_command(
    *,
    url: str | None = None,
    iso: str | None = None,
    sha256: str | None = None,
    name: str | None = None,
    wimboot: str | None = None,
) -> list[str]:
    if not url and not iso:
        raise MediaJobError("Windows import requires a Microsoft ISO URL or local ISO path")
    command = [*_base_command(), "windows"]
    if url:
        command.extend(["--url", url])
    if iso:
        command.extend(["--iso", iso])
    if sha256:
        command.extend(["--sha256", sha256])
    if name:
        command.extend(["--name", name])
    if wimboot:
        command.extend(["--wimboot", wimboot])
    return command


def debian_command(*, version: str, name: str | None = None, url: str | None = None) -> list[str]:
    command = [*_base_command(), "debian", "--version", version]
    if name:
        command.extend(["--name", name])
    if url:
        command.extend(["--url", url])
    return command


def debian_set_command(*, versions: list[str]) -> list[str]:
    command = [*_base_command(), "debian-set"]
    if versions:
        command.extend(["--versions", *versions])
    return command


_RHEL_SUBCOMMAND = {
    "rocky": ("rocky", "rocky-set"),
    "almalinux": ("almalinux", "almalinux-set"),
    "fedora": ("fedora", "fedora-set"),
}


def rhel_family_command(family: str, *, version: str, name: str | None = None) -> list[str]:
    if family not in _RHEL_SUBCOMMAND:
        raise MediaJobError(f"Unsupported RHEL family: {family}")
    subcmd, _ = _RHEL_SUBCOMMAND[family]
    command = [*_base_command(), subcmd, "--version", version]
    if name:
        command.extend(["--name", name])
    return command


def rhel_family_set_command(family: str, *, versions: list[str]) -> list[str]:
    if family not in _RHEL_SUBCOMMAND:
        raise MediaJobError(f"Unsupported RHEL family: {family}")
    _, set_subcmd = _RHEL_SUBCOMMAND[family]
    command = [*_base_command(), set_subcmd]
    if versions:
        command.extend(["--versions", *versions])
    return command


def queue_media_import(
    db: Session,
    *,
    kind: str,
    command: list[str],
    metadata: dict[str, Any] | None = None,
) -> MediaImportRun:
    run = MediaImportRun(
        kind=kind,
        state=MediaImportState.QUEUED,
        command=command,
        metadata_json=metadata or {},
    )
    db.add(run)
    db.flush()
    return run


def recent_media_imports(db: Session, limit: int = 10) -> list[MediaImportRun]:
    return db.scalars(
        select(MediaImportRun)
        .order_by(MediaImportRun.queued_at.desc(), MediaImportRun.id.desc())
        .limit(limit)
    ).all()


_PROGRESS_RE = re.compile(r"^PXE_PROGRESS:\s*(\d{1,3})(?:\s+(.*))?$")
_PHASE_RE = re.compile(r"^PXE_PHASE:\s*(.*)$")
_PROGRESS_COMMIT_INTERVAL = 0.75  # seconds between DB writes for progress noise


def _update_progress(run: MediaImportRun, *, percent: int | None = None, phase: str | None = None) -> bool:
    """Merge progress fields into metadata_json. Returns True if something changed."""
    meta = dict(run.metadata_json or {})
    changed = False
    if percent is not None and meta.get("progress_percent") != percent:
        meta["progress_percent"] = percent
        changed = True
    if phase is not None and meta.get("progress_phase") != phase:
        meta["progress_phase"] = phase
        changed = True
    if changed:
        run.metadata_json = meta
    return changed


def _stream_subprocess(run: MediaImportRun, db: Session) -> int:
    process = subprocess.Popen(
        run.command,
        cwd=os.getcwd(),
        env=os.environ.copy(),
        text=True,
        bufsize=1,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, ("stdout", stdout_buf))
    selector.register(process.stderr, selectors.EVENT_READ, ("stderr", stderr_buf))

    deadline = time.monotonic() + IMPORT_TIMEOUT_SECONDS
    last_commit = 0.0

    while selector.get_map():
        if time.monotonic() > deadline:
            process.kill()
            raise subprocess.TimeoutExpired(run.command, IMPORT_TIMEOUT_SECONDS)

        events = selector.select(timeout=0.5)
        for key, _ in events:
            stream_name, buf = key.data
            line = key.fileobj.readline()
            if not line:
                selector.unregister(key.fileobj)
                key.fileobj.close()
                continue
            buf.write(line)
            stripped = line.rstrip("\n")
            progress_match = _PROGRESS_RE.match(stripped)
            phase_match = _PHASE_RE.match(stripped)
            if progress_match:
                pct = int(progress_match.group(1))
                hint = (progress_match.group(2) or "").strip() or None
                _update_progress(run, percent=pct, phase=hint)
            elif phase_match:
                _update_progress(run, phase=phase_match.group(1).strip())

        now = time.monotonic()
        if now - last_commit >= _PROGRESS_COMMIT_INTERVAL:
            db.commit()
            last_commit = now

    return_code = process.wait(timeout=10)
    run.stdout = stdout_buf.getvalue()[-OUTPUT_LIMIT:]
    run.stderr = stderr_buf.getvalue()[-OUTPUT_LIMIT:]
    return return_code


def run_media_import(run_id: int) -> None:
    with SessionLocal() as db:
        run = db.get(MediaImportRun, run_id)
        if not run or run.state != MediaImportState.QUEUED:
            return

        run.state = MediaImportState.RUNNING
        run.started_at = utcnow()
        _update_progress(run, percent=0, phase="Starting")
        db.commit()

        try:
            return_code = _stream_subprocess(run, db)
            run.return_code = return_code
            success = return_code == 0
            run.state = MediaImportState.SUCCESS if success else MediaImportState.FAILED
            if success:
                _update_progress(run, percent=100, phase="Completed")
                ensure_default_profiles(db)
                ensure_unattended_default_profile(db)
            else:
                _update_progress(run, phase=f"Exited with code {return_code}")
        except subprocess.TimeoutExpired:
            run.return_code = None
            run.error = f"Timed out after {IMPORT_TIMEOUT_SECONDS} seconds"
            run.state = MediaImportState.FAILED
            _update_progress(run, phase="Timed out")
        except Exception as exc:  # noqa: BLE001 - persisted for operator diagnostics.
            run.error = str(exc)
            run.state = MediaImportState.FAILED
            _update_progress(run, phase=f"Error: {exc}")
        finally:
            run.completed_at = utcnow()
            db.commit()
