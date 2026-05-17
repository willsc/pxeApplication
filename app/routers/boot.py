from __future__ import annotations

from datetime import timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models import BuildState, Host, HostState, utcnow
from app.security import generate_install_token
from app.services.audit import record_event
from app.services.builds import finish_latest_build_record, start_build_record
from app.services.ansible_runner import queue_ansible_run, run_ansible_run
from app.services.config_render import ConfigRenderError, render_config
from app.services.ipxe import (
    bootstrap_script,
    install_script,
    localboot_script,
    normalize_mac,
    registered_script,
    register_then_localboot_script,
    unknown_menu_script,
)
from app.services.unattended import apply_unattended_profile
from app.settings import settings


router = APIRouter(tags=["boot"])


def _ipxe_response(body: str, status_code: int = 200) -> PlainTextResponse:
    return PlainTextResponse(body, status_code=status_code, media_type="text/plain")


def _get_host_by_token(db: Session, token: str) -> Host | None:
    return db.scalar(select(Host).where(Host.install_token == token))


@router.get("/boot.ipxe")
def boot_ipxe(
    request: Request,
    mac: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if not mac:
        script = bootstrap_script(settings)
        return _ipxe_response(script.body, script.status_code)

    try:
        normalized_mac = normalize_mac(mac)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    host = db.scalar(select(Host).where(Host.mac == normalized_mac))
    if not host:
        host = Host(mac=normalized_mac, state=HostState.PENDING, install_token=generate_install_token())
        db.add(host)
        db.flush()
        if apply_unattended_profile(db, host):
            host.last_boot_at = utcnow()
            record_event(
                db,
                event_type="unattended_enroll",
                request=request,
                host=host,
                payload={"profile_id": host.profile_id},
            )
            script = install_script(host, settings)
            db.commit()
            return _ipxe_response(script.body, script.status_code)

        record_event(
            db,
            event_type="unknown_boot",
            request=request,
            host=host,
            payload={"policy": settings.unknown_host_policy, "registered": True},
        )
        if settings.unknown_host_policy == "localboot":
            script = localboot_script("Unknown host; booting from local disk.")
        elif settings.unknown_host_policy == "register":
            script = register_then_localboot_script(normalized_mac, settings)
        else:
            script = unknown_menu_script(normalized_mac, settings)
        db.commit()
        return _ipxe_response(script.body, script.status_code)

    host.last_boot_at = utcnow()
    if host.state == HostState.PENDING and apply_unattended_profile(db, host):
        record_event(
            db,
            event_type="unattended_profile_assigned",
            request=request,
            host=host,
            payload={"profile_id": host.profile_id},
        )
        script = install_script(host, settings)
        db.commit()
        return _ipxe_response(script.body, script.status_code)

    record_event(
        db,
        event_type="boot_script",
        request=request,
        host=host,
        payload={"state": host.state.value},
    )
    if host.state in {HostState.READY, HostState.INSTALLING}:
        script = install_script(host, settings)
    elif host.state == HostState.PENDING:
        script = registered_script(host)
    else:
        script = localboot_script(f"Host state is {host.state.value}; no install will be started.")
    db.commit()
    return _ipxe_response(script.body, script.status_code)


@router.get("/api/boot/register")
def register_host(
    request: Request,
    mac: str = Query(...),
    db: Session = Depends(get_db),
):
    try:
        normalized_mac = normalize_mac(mac)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    host = db.scalar(select(Host).where(Host.mac == normalized_mac))
    if not host:
        host = Host(mac=normalized_mac, state=HostState.PENDING, install_token=generate_install_token())
        db.add(host)
        event_type = "registered"
    else:
        event_type = "register_seen_existing"
    db.flush()
    if apply_unattended_profile(db, host):
        event_type = "registered_unattended" if event_type == "registered" else "register_seen_existing_unattended"
        record_event(db, event_type=event_type, request=request, host=host, payload={"profile_id": host.profile_id})
        script = install_script(host, settings)
        db.commit()
        return _ipxe_response(script.body, script.status_code)
    record_event(db, event_type=event_type, request=request, host=host)
    db.commit()
    script = registered_script(host)
    return _ipxe_response(script.body, script.status_code)


@router.get("/api/boot/config/{token}")
def boot_config(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    host = _get_host_by_token(db, token)
    if not host:
        record_event(db, event_type="config_token_not_found", request=request, token=token)
        db.commit()
        raise HTTPException(status_code=404, detail="Install token not found")
    if host.state not in {HostState.READY, HostState.INSTALLING}:
        record_event(
            db,
            event_type="config_rejected",
            request=request,
            host=host,
            token=token,
            payload={"state": host.state.value},
        )
        db.commit()
        raise HTTPException(status_code=409, detail=f"Host state is {host.state.value}")

    was_ready = host.state == HostState.READY
    if was_ready:
        host.state = HostState.INSTALLING
    try:
        rendered, media_type = render_config(host, settings)
    except ConfigRenderError as exc:
        host.state = HostState.FAILED
        record_event(
            db,
            event_type="config_render_failed",
            request=request,
            host=host,
            token=token,
            payload={"error": str(exc)},
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if was_ready:
        start_build_record(db, host, token)
    record_event(db, event_type="config_served", request=request, host=host, token=token)
    db.commit()
    return Response(rendered, media_type=media_type)


@router.get("/api/boot/seed/{token}/meta-data")
def ubuntu_meta_data(token: str, request: Request, db: Session = Depends(get_db)):
    host = _get_host_by_token(db, token)
    if not host:
        record_event(db, event_type="seed_meta_token_not_found", request=request, token=token)
        db.commit()
        raise HTTPException(status_code=404, detail="Install token not found")
    record_event(db, event_type="seed_meta_served", request=request, host=host, token=token)
    db.commit()
    instance_id = f"pxe-app-{host.id}-{host.install_token[:8]}"
    hostname = host.hostname or f"host-{host.mac.replace(':', '')}"
    return PlainTextResponse(f"instance-id: {instance_id}\nlocal-hostname: {hostname}\n")


@router.get("/api/boot/seed/{token}/user-data")
def ubuntu_user_data(token: str, request: Request, db: Session = Depends(get_db)):
    return boot_config(token, request, db)


@router.post("/api/boot/callback/{token}")
async def boot_callback(
    token: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    host = _get_host_by_token(db, token)
    if not host:
        record_event(db, event_type="callback_token_not_found", request=request, token=token)
        db.commit()
        raise HTTPException(status_code=404, detail="Install token not found")

    payload: dict[str, Any]
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form)
        payload.update(dict(request.query_params))

    raw_status = str(payload.get("status", "")).lower()
    if raw_status in {"done", "success", "ok", "provisioned"}:
        host.state = HostState.PROVISIONED
        host.provisioned_at = utcnow().astimezone(timezone.utc)
        callback_status = "done"
        build_state = BuildState.PROVISIONED
    elif raw_status in {"failed", "fail", "error"}:
        host.state = HostState.FAILED
        callback_status = "failed"
        build_state = BuildState.FAILED
    else:
        record_event(
            db,
            event_type="callback_rejected",
            request=request,
            host=host,
            token=token,
            payload={"payload": payload},
        )
        db.commit()
        raise HTTPException(status_code=400, detail="Callback requires status=done or status=failed")

    build_record = finish_latest_build_record(db, host, build_state)
    ansible_run = None
    if build_state == BuildState.PROVISIONED:
        ansible_run = queue_ansible_run(db, host, trigger="callback", build_record_id=build_record.id)
    host.install_token = generate_install_token()
    record_event(
        db,
        event_type="callback",
        request=request,
        host=host,
        token=token,
        payload={"status": callback_status, "payload": payload},
    )
    db.commit()
    if ansible_run:
        background_tasks.add_task(run_ansible_run, ansible_run.id)
    return JSONResponse({"ok": True, "host_id": host.id, "state": host.state.value})
