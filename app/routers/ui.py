from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dependencies import get_current_user_or_redirect, get_db, require_csrf
from app.models import (
    AnsibleRun,
    AppSetting,
    Asset,
    AssetStatus,
    BootEvent,
    BuildRecord,
    Host,
    HostState,
    Image,
    OSType,
    PostInstallConfig,
    Profile,
    ProfileExtras,
    User,
)
from app.security import generate_install_token
from app.services.ansible_runner import AnsibleRunError, queue_ansible_run, run_ansible_run, _safe_relative_path
from app.services.config_render import ConfigRenderError, _safe_template_path
from app.services.ipxe import normalize_mac
from app.distros import CATALOG, by_slug
from app.services.dhcp_probe import mark_probe_starting, run_dhcp_probe
from app.services.setup_status import (
    DHCP_MODE_OVERRIDE_KEY,
    DHCP_PROBE_SETTING_KEY,
    HOST_IP_OVERRIDE_KEY,
    compute_setup_status,
)
from app.services.media_jobs import (
    MediaJobError,
    bootloaders_command,
    debian_command,
    debian_set_command,
    queue_media_import,
    recent_media_imports,
    rhel_family_command,
    rhel_family_set_command,
    run_media_import,
    ubuntu_command,
    ubuntu_set_command,
    windows_command,
)
from app.services.profiles import (
    blocking_profiles_for_image,
    delete_unassigned_auto_profiles,
    ensure_default_profile,
    ensure_default_profiles,
)
from app.services.unattended import (
    ensure_unattended_default_profile,
    get_unattended_default_profile,
    set_unattended_default_profile,
)
from app.settings import settings
from app.templating import context, templates


router = APIRouter(tags=["ui"])

UBUNTU_MEDIA_VERSIONS = ["22", "24", "26"]


def _json_field(value: str | None, field_name: str) -> dict[str, Any]:
    if not value or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a JSON object")
    return parsed


def _optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _validate_template_path(template_path: str | None) -> None:
    if not template_path:
        return
    try:
        _safe_template_path(template_path)
    except ConfigRenderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_or_redirect),
):
    counts = {state.value: 0 for state in HostState}
    for state, count in db.execute(select(Host.state, func.count(Host.id)).group_by(Host.state)):
        counts[state.value] = count
    recent_events = db.scalars(select(BootEvent).order_by(BootEvent.created_at.desc()).limit(10)).all()
    return templates.TemplateResponse(
        "index.html",
        context(request, user, counts=counts, recent_events=recent_events),
    )


@router.get("/hosts")
def hosts_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_or_redirect),
):
    hosts = db.scalars(select(Host).order_by(Host.created_at.desc())).all()
    profiles = db.scalars(select(Profile).order_by(Profile.name)).all()
    return templates.TemplateResponse(
        "hosts.html",
        context(request, user, hosts=hosts, profiles=profiles, error=None),
    )


@router.post("/hosts")
def create_host_form(
    request: Request,
    mac: str = Form(...),
    hostname: str | None = Form(None),
    profile_id: str | None = Form(None),
    variables_json: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    try:
        normalized_mac = normalize_mac(mac)
        parsed_profile_id = _optional_int(profile_id)
        if parsed_profile_id and not db.get(Profile, parsed_profile_id):
            raise HTTPException(status_code=400, detail="Profile does not exist")
        host = Host(
            mac=normalized_mac,
            hostname=hostname or None,
            profile_id=parsed_profile_id,
            state=HostState.READY if parsed_profile_id else HostState.PENDING,
            install_token=generate_install_token(),
            variables=_json_field(variables_json, "Host variables"),
        )
        db.add(host)
        db.commit()
    except (HTTPException, IntegrityError, ValueError) as exc:
        db.rollback()
        profiles = db.scalars(select(Profile).order_by(Profile.name)).all()
        hosts = db.scalars(select(Host).order_by(Host.created_at.desc())).all()
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return templates.TemplateResponse(
            "hosts.html",
            context(request, user, hosts=hosts, profiles=profiles, error=detail),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/hosts", status_code=status.HTTP_303_SEE_OTHER)


def _host_detail_response(
    request: Request,
    user: User,
    host: Host,
    db: Session,
    *,
    variables_json: str | None = None,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
):
    profiles = db.scalars(select(Profile).order_by(Profile.name)).all()
    events = db.scalars(
        select(BootEvent).where(BootEvent.host_id == host.id).order_by(BootEvent.created_at.desc()).limit(50)
    ).all()
    builds = db.scalars(
        select(BuildRecord)
        .where(BuildRecord.host_id == host.id)
        .order_by(BuildRecord.started_at.desc(), BuildRecord.id.desc())
        .limit(20)
    ).all()
    ansible_runs = db.scalars(
        select(AnsibleRun)
        .where(AnsibleRun.host_id == host.id)
        .order_by(AnsibleRun.queued_at.desc(), AnsibleRun.id.desc())
        .limit(20)
    ).all()
    return templates.TemplateResponse(
        "host_detail.html",
        context(
            request,
            user,
            host=host,
            profiles=profiles,
            states=list(HostState),
            events=events,
            builds=builds,
            ansible_runs=ansible_runs,
            asset=host.asset,
            variables_json=variables_json
            if variables_json is not None
            else json.dumps(host.variables or {}, indent=2),
            error=error,
        ),
        status_code=status_code,
    )


@router.get("/hosts/{host_id}")
def host_detail_page(
    host_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_or_redirect),
):
    host = db.get(Host, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    return _host_detail_response(request, user, host, db)


@router.post("/hosts/{host_id}")
def update_host_form(
    host_id: int,
    request: Request,
    hostname: str | None = Form(None),
    profile_id: str | None = Form(None),
    state: HostState = Form(...),
    variables_json: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    host = db.get(Host, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    try:
        parsed_profile_id = _optional_int(profile_id)
        if parsed_profile_id and not db.get(Profile, parsed_profile_id):
            raise HTTPException(status_code=400, detail="Profile does not exist")
        previous_state = host.state
        previous_profile_id = host.profile_id
        host.hostname = hostname or None
        host.profile_id = parsed_profile_id
        host.state = state
        host.variables = _json_field(variables_json, "Host variables")
        if previous_state != state or previous_profile_id != parsed_profile_id:
            host.install_token = generate_install_token()
        db.commit()
    except (HTTPException, ValueError) as exc:
        db.rollback()
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _host_detail_response(
            request,
            user,
            host,
            db,
            variables_json=variables_json or "{}",
            error=detail,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(f"/hosts/{host.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/hosts/{host_id}/request-install")
def request_install_form(
    host_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    host = db.get(Host, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if not host.profile_id:
        raise HTTPException(status_code=400, detail="Assign a profile before requesting install")
    host.state = HostState.READY
    host.install_token = generate_install_token()
    host.provisioned_at = None
    db.commit()
    return RedirectResponse(f"/hosts/{host.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/hosts/{host_id}/decommission")
def decommission_host_form(
    host_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    host = db.get(Host, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    host.state = HostState.DECOMMISSIONED
    host.install_token = generate_install_token()
    db.commit()
    return RedirectResponse(f"/hosts/{host.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/hosts/{host_id}/run-ansible")
def run_host_ansible_form(
    host_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    host = db.get(Host, host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    try:
        run = queue_ansible_run(db, host, trigger="manual")
        if not run:
            raise AnsibleRunError("Host profile has no enabled Ansible post-install config")
        db.commit()
        background_tasks.add_task(run_ansible_run, run.id)
    except AnsibleRunError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/hosts/{host.id}", status_code=status.HTTP_303_SEE_OTHER)


def _images_context(
    request: Request,
    user: User,
    db: Session,
    *,
    error: str | None = None,
):
    setup = compute_setup_status(db)
    images = db.scalars(select(Image).order_by(Image.name)).all()
    return context(
        request,
        user,
        images=images,
        os_types=list(OSType),
        ubuntu_versions=UBUNTU_MEDIA_VERSIONS,
        media_runs=recent_media_imports(db),
        catalog=CATALOG,
        setup=setup,
        error=error,
    )


@router.get("/images")
def images_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_or_redirect),
):
    return templates.TemplateResponse("images.html", _images_context(request, user, db))


def _images_response(
    request: Request,
    user: User,
    db: Session,
    *,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
):
    return templates.TemplateResponse(
        "images.html",
        _images_context(request, user, db, error=error),
        status_code=status_code,
    )


def _queue_import_and_redirect(
    db: Session,
    background_tasks: BackgroundTasks,
    *,
    kind: str,
    command: list[str],
    metadata: dict[str, Any] | None = None,
):
    run = queue_media_import(db, kind=kind, command=command, metadata=metadata)
    db.commit()
    background_tasks.add_task(run_media_import, run.id)
    return RedirectResponse("/images", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/media-imports/bootloaders")
def import_bootloaders_form(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    return _queue_import_and_redirect(
        db,
        background_tasks,
        kind="bootloaders",
        command=bootloaders_command(),
    )


@router.post("/media-imports/ubuntu")
def import_ubuntu_form(
    request: Request,
    background_tasks: BackgroundTasks,
    edition: str = Form(...),
    version: str = Form(...),
    name: str | None = Form(None),
    url: str | None = Form(None),
    sha256: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    try:
        command = ubuntu_command(
            edition=edition,
            version=version,
            name=name or None,
            url=url or None,
            sha256=sha256 or None,
        )
    except MediaJobError as exc:
        return _images_response(request, user, db, error=str(exc), status_code=status.HTTP_400_BAD_REQUEST)
    return _queue_import_and_redirect(
        db,
        background_tasks,
        kind=f"ubuntu-{edition}",
        command=command,
        metadata={"edition": edition, "version": version, "name": name or None},
    )


@router.post("/media-imports/ubuntu-set")
def import_ubuntu_set_form(
    request: Request,
    background_tasks: BackgroundTasks,
    edition: str = Form(...),
    versions: str = Form("22 24 26"),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    parsed_versions = [value.strip() for value in versions.replace(",", " ").split() if value.strip()]
    try:
        command = ubuntu_set_command(edition=edition, versions=parsed_versions)
    except MediaJobError as exc:
        return _images_response(request, user, db, error=str(exc), status_code=status.HTTP_400_BAD_REQUEST)
    return _queue_import_and_redirect(
        db,
        background_tasks,
        kind=f"ubuntu-{edition}-set",
        command=command,
        metadata={"edition": edition, "versions": parsed_versions},
    )


@router.post("/network/dhcp-probe")
def trigger_dhcp_probe(
    background_tasks: BackgroundTasks,
    _: User = Depends(require_csrf),
):
    mark_probe_starting()
    background_tasks.add_task(run_dhcp_probe)
    return RedirectResponse("/images", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/network/dhcp-mode")
def set_dhcp_mode_form(
    mode: str = Form(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    if mode not in {"proxy", "server"}:
        raise HTTPException(status_code=400, detail="mode must be proxy or server")
    setting = db.get(AppSetting, DHCP_MODE_OVERRIDE_KEY)
    if setting:
        setting.value_json = {"mode": mode}
    else:
        db.add(AppSetting(key=DHCP_MODE_OVERRIDE_KEY, value_json={"mode": mode}))
    db.commit()
    return RedirectResponse(f"/images?saved=dhcp_mode:{mode}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/network/host-ip")
def set_host_ip_form(
    host_ip: str = Form(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    from ipaddress import AddressValueError, IPv4Address

    candidate = (host_ip or "").strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="host IP cannot be empty")
    try:
        IPv4Address(candidate)
    except (AddressValueError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid IPv4 address: {candidate}") from exc
    setting = db.get(AppSetting, HOST_IP_OVERRIDE_KEY)
    if setting:
        setting.value_json = {"host_ip": candidate}
    else:
        db.add(AppSetting(key=HOST_IP_OVERRIDE_KEY, value_json={"host_ip": candidate}))
    db.commit()
    return RedirectResponse(f"/images?saved=host_ip:{candidate}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/api/network/dhcp-probe")
def dhcp_probe_status_json(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user_or_redirect),
):
    setting = db.get(AppSetting, DHCP_PROBE_SETTING_KEY)
    payload = setting.value_json if setting else {}
    return {
        "running": bool(payload.get("running")),
        "offers": payload.get("offers") or [],
        "checked_at": payload.get("checked_at"),
        "error": payload.get("error"),
        "source": payload.get("source"),
    }


@router.get("/api/media-imports")
def media_imports_status_json(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user_or_redirect),
):
    runs = recent_media_imports(db)
    return {
        "runs": [
            {
                "id": run.id,
                "kind": run.kind,
                "state": run.state.value,
                "progress_percent": (run.metadata_json or {}).get("progress_percent"),
                "progress_phase": (run.metadata_json or {}).get("progress_phase"),
                "queued_at": run.queued_at.isoformat() if run.queued_at else None,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "return_code": run.return_code,
                "error": run.error,
            }
            for run in runs
        ]
    }


@router.post("/media-imports/distro")
def import_distro_form(
    request: Request,
    background_tasks: BackgroundTasks,
    slug: str = Form(...),
    versions: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    try:
        distro = by_slug(slug)
    except KeyError:
        return _images_response(
            request, user, db, error=f"Unknown distro: {slug}", status_code=status.HTTP_400_BAD_REQUEST
        )

    chosen_versions = [v for v in versions if v]
    if not chosen_versions:
        return _images_response(
            request,
            user,
            db,
            error=f"Pick at least one version for {distro.label}.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    valid_keys = {v.key for v in distro.versions}
    bad = [v for v in chosen_versions if v not in valid_keys]
    if bad:
        return _images_response(
            request,
            user,
            db,
            error=f"Unknown versions for {distro.label}: {', '.join(bad)}",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        if distro.slug == "ubuntu-desktop":
            command = ubuntu_set_command(edition="desktop", versions=chosen_versions)
        elif distro.slug == "ubuntu-server":
            command = ubuntu_set_command(edition="server", versions=chosen_versions)
        elif distro.slug == "debian":
            command = debian_set_command(versions=chosen_versions)
        elif distro.slug in {"rocky", "almalinux", "fedora"}:
            command = rhel_family_set_command(distro.slug, versions=chosen_versions)
        else:
            raise MediaJobError(f"Distro {distro.slug} cannot be imported through this form")
    except MediaJobError as exc:
        return _images_response(request, user, db, error=str(exc), status_code=status.HTTP_400_BAD_REQUEST)

    return _queue_import_and_redirect(
        db,
        background_tasks,
        kind=f"{distro.slug}-set",
        command=command,
        metadata={"slug": distro.slug, "versions": chosen_versions},
    )


@router.post("/media-imports/windows")
def import_windows_form(
    request: Request,
    background_tasks: BackgroundTasks,
    url: str | None = Form(None),
    iso: str | None = Form(None),
    sha256: str | None = Form(None),
    name: str | None = Form(None),
    wimboot: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    try:
        command = windows_command(
            url=url or None,
            iso=iso or None,
            sha256=sha256 or None,
            name=name or None,
            wimboot=wimboot or None,
        )
    except MediaJobError as exc:
        return _images_response(request, user, db, error=str(exc), status_code=status.HTTP_400_BAD_REQUEST)
    return _queue_import_and_redirect(
        db,
        background_tasks,
        kind="windows",
        command=command,
        metadata={"url": bool(url), "iso": iso or None, "name": name or "windows-11"},
    )


@router.post("/images")
def create_image_form(
    request: Request,
    name: str = Form(...),
    os_type: OSType = Form(...),
    architecture: str = Form("x86_64"),
    kernel_path: str | None = Form(None),
    initrd_path: str | None = Form(None),
    repo_url: str | None = Form(None),
    bootloader_path: str | None = Form(None),
    wim_path: str | None = Form(None),
    bcd_path: str | None = Form(None),
    boot_sdi_path: str | None = Form(None),
    extra_kernel_args: str | None = Form(None),
    metadata_json: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    try:
        image = Image(
            name=name,
            os_type=os_type,
            architecture=architecture,
            kernel_path=kernel_path or None,
            initrd_path=initrd_path or None,
            repo_url=repo_url or None,
            bootloader_path=bootloader_path or None,
            wim_path=wim_path or None,
            bcd_path=bcd_path or None,
            boot_sdi_path=boot_sdi_path or None,
            extra_kernel_args=extra_kernel_args or None,
            metadata_json=_json_field(metadata_json, "Image metadata"),
        )
        db.add(image)
        db.flush()
        ensure_default_profile(db, image)
        ensure_unattended_default_profile(db)
        db.commit()
    except (IntegrityError, HTTPException) as exc:
        db.rollback()
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _images_response(request, user, db, error=detail, status_code=status.HTTP_400_BAD_REQUEST)
    return RedirectResponse("/images", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/images/{image_id}")
def image_detail_page(
    image_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_or_redirect),
):
    image = db.get(Image, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    profile_count = db.scalar(select(func.count(Profile.id)).where(Profile.image_id == image.id)) or 0
    blocking_profile_count = len(blocking_profiles_for_image(image))
    return templates.TemplateResponse(
        request,
        "image_detail.html",
        context(
            request,
            user,
            image=image,
            os_types=list(OSType),
            metadata_json=json.dumps(image.metadata_json or {}, indent=2),
            profile_count=profile_count,
            blocking_profile_count=blocking_profile_count,
            error=None,
        ),
    )


@router.post("/images/{image_id}")
def update_image_form(
    image_id: int,
    request: Request,
    name: str = Form(...),
    os_type: OSType = Form(...),
    architecture: str = Form("x86_64"),
    kernel_path: str | None = Form(None),
    initrd_path: str | None = Form(None),
    repo_url: str | None = Form(None),
    bootloader_path: str | None = Form(None),
    wim_path: str | None = Form(None),
    bcd_path: str | None = Form(None),
    boot_sdi_path: str | None = Form(None),
    extra_kernel_args: str | None = Form(None),
    metadata_json: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    image = db.get(Image, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    try:
        image.name = name
        image.os_type = os_type
        image.architecture = architecture
        image.kernel_path = kernel_path or None
        image.initrd_path = initrd_path or None
        image.repo_url = repo_url or None
        image.bootloader_path = bootloader_path or None
        image.wim_path = wim_path or None
        image.bcd_path = bcd_path or None
        image.boot_sdi_path = boot_sdi_path or None
        image.extra_kernel_args = extra_kernel_args or None
        image.metadata_json = _json_field(metadata_json, "Image metadata")
        ensure_default_profile(db, image)
        ensure_unattended_default_profile(db)
        db.commit()
    except (IntegrityError, HTTPException) as exc:
        db.rollback()
        profile_count = db.scalar(select(func.count(Profile.id)).where(Profile.image_id == image.id)) or 0
        blocking_profile_count = len(blocking_profiles_for_image(image))
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return templates.TemplateResponse(
            request,
            "image_detail.html",
            context(
                request,
                user,
                image=image,
                os_types=list(OSType),
                metadata_json=metadata_json or "{}",
                profile_count=profile_count,
                blocking_profile_count=blocking_profile_count,
                error=detail,
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(f"/images/{image.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/images/{image_id}/delete")
def delete_image_form(
    image_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    image = db.get(Image, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    profile_count = db.scalar(select(func.count(Profile.id)).where(Profile.image_id == image.id)) or 0
    blocking_profile_count = len(blocking_profiles_for_image(image))
    if blocking_profile_count:
        return templates.TemplateResponse(
            request,
            "image_detail.html",
            context(
                request,
                user,
                image=image,
                os_types=list(OSType),
                metadata_json=json.dumps(image.metadata_json or {}, indent=2),
                profile_count=profile_count,
                blocking_profile_count=blocking_profile_count,
                error="Image is still used by profiles",
            ),
            status_code=status.HTTP_409_CONFLICT,
        )
    delete_unassigned_auto_profiles(db, image)
    db.delete(image)
    db.commit()
    return RedirectResponse("/images", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/profiles")
def profiles_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_or_redirect),
):
    if ensure_default_profiles(db):
        db.commit()
    default_profile = ensure_unattended_default_profile(db)
    if default_profile:
        db.commit()
    profiles = db.scalars(select(Profile).order_by(Profile.name)).all()
    images = db.scalars(select(Image).order_by(Image.name)).all()
    return templates.TemplateResponse(
        "profiles.html",
        context(
            request,
            user,
            profiles=profiles,
            images=images,
            default_profile=default_profile,
            unattended_auto_enroll=settings.unattended_auto_enroll,
            error=None,
        ),
    )


@router.post("/profiles/{profile_id}/set-unattended-default")
def set_unattended_default_profile_form(
    profile_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    set_unattended_default_profile(db, profile)
    db.commit()
    return RedirectResponse("/profiles", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/profiles")
def create_profile_form(
    request: Request,
    name: str = Form(...),
    image_id: int = Form(...),
    template_path: str | None = Form(None),
    variables_json: str | None = Form(None),
    authorized_keys: str | None = Form(None),
    root_password: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    try:
        if not db.get(Image, image_id):
            raise HTTPException(status_code=400, detail="Image does not exist")
        _validate_template_path(template_path)
        profile = Profile(
            name=name,
            image_id=image_id,
            template_path=template_path or None,
            variables=_json_field(variables_json, "Profile variables"),
            authorized_keys=authorized_keys or None,
            root_password=root_password or None,
        )
        db.add(profile)
        db.commit()
    except (IntegrityError, HTTPException) as exc:
        db.rollback()
        profiles = db.scalars(select(Profile).order_by(Profile.name)).all()
        images = db.scalars(select(Image).order_by(Image.name)).all()
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return templates.TemplateResponse(
            "profiles.html",
            context(
                request,
                user,
                profiles=profiles,
                images=images,
                default_profile=get_unattended_default_profile(db),
                unattended_auto_enroll=settings.unattended_auto_enroll,
                error=detail,
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/profiles", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/profiles/{profile_id}")
def profile_detail_page(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_or_redirect),
):
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    images = db.scalars(select(Image).order_by(Image.name)).all()
    host_count = db.scalar(select(func.count(Host.id)).where(Host.profile_id == profile.id)) or 0
    return templates.TemplateResponse(
        request,
        "profile_detail.html",
        context(
            request,
            user,
            profile=profile,
            extras=profile.extras,
            images=images,
            variables_json=json.dumps(profile.variables or {}, indent=2),
            host_count=host_count,
            error=None,
        ),
    )


@router.post("/profiles/{profile_id}/extras")
def save_profile_extras_form(
    profile_id: int,
    extra_packages: str | None = Form(None),
    finish_script_bash: str | None = Form(None),
    finish_script_powershell: str | None = Form(None),
    ansible_pull_url: str | None = Form(None),
    ansible_pull_playbook: str | None = Form(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    extras = profile.extras
    if not extras:
        extras = ProfileExtras(profile=profile)
        db.add(extras)
    extras.extra_packages = (extra_packages or "").strip() or None
    extras.finish_script_bash = finish_script_bash or None
    extras.finish_script_powershell = finish_script_powershell or None
    extras.ansible_pull_url = (ansible_pull_url or "").strip() or None
    extras.ansible_pull_playbook = (ansible_pull_playbook or "").strip() or None
    db.commit()
    return RedirectResponse(f"/profiles/{profile.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/profiles/{profile_id}")
def update_profile_form(
    profile_id: int,
    request: Request,
    name: str = Form(...),
    image_id: int = Form(...),
    template_path: str | None = Form(None),
    variables_json: str | None = Form(None),
    authorized_keys: str | None = Form(None),
    root_password: str | None = Form(None),
    clear_root_password: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    try:
        if not db.get(Image, image_id):
            raise HTTPException(status_code=400, detail="Image does not exist")
        _validate_template_path(template_path)
        profile.name = name
        profile.image_id = image_id
        profile.template_path = template_path or None
        profile.variables = _json_field(variables_json, "Profile variables")
        profile.authorized_keys = authorized_keys or None
        if clear_root_password == "on":
            profile.root_password = None
        elif root_password:
            profile.root_password = root_password
        db.commit()
    except (IntegrityError, HTTPException) as exc:
        db.rollback()
        images = db.scalars(select(Image).order_by(Image.name)).all()
        host_count = db.scalar(select(func.count(Host.id)).where(Host.profile_id == profile.id)) or 0
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return templates.TemplateResponse(
            request,
            "profile_detail.html",
            context(
                request,
                user,
                profile=profile,
                extras=profile.extras,
                images=images,
                variables_json=variables_json or "{}",
                host_count=host_count,
                error=detail,
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(f"/profiles/{profile.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/profiles/{profile_id}/delete")
def delete_profile_form(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    images = db.scalars(select(Image).order_by(Image.name)).all()
    host_count = db.scalar(select(func.count(Host.id)).where(Host.profile_id == profile.id)) or 0
    if host_count:
        return templates.TemplateResponse(
            request,
            "profile_detail.html",
            context(
                request,
                user,
                profile=profile,
                extras=profile.extras,
                images=images,
                variables_json=json.dumps(profile.variables or {}, indent=2),
                host_count=host_count,
                error="Profile is still assigned to hosts",
            ),
            status_code=status.HTTP_409_CONFLICT,
        )
    db.delete(profile)
    db.commit()
    return RedirectResponse("/profiles", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/profiles/{profile_id}/post-install")
def post_install_page(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_or_redirect),
):
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    config = profile.post_install_config
    return templates.TemplateResponse(
        "post_install.html",
        context(
            request,
            user,
            profile=profile,
            config=config,
            inventory_vars_json=json.dumps(config.inventory_vars if config else {}, indent=2),
            extra_vars_json=json.dumps(config.extra_vars if config else {}, indent=2),
            error=None,
        ),
    )


@router.post("/profiles/{profile_id}/post-install")
def save_post_install_form(
    profile_id: int,
    request: Request,
    enabled: str | None = Form(None),
    playbook_path: str = Form(...),
    ssh_user: str = Form("root"),
    ssh_port: int = Form(22),
    ssh_private_key_path: str | None = Form(None),
    become: str | None = Form(None),
    host_key_checking: str | None = Form(None),
    inventory_groups: str | None = Form(None),
    inventory_vars_json: str | None = Form(None),
    extra_vars_json: str | None = Form(None),
    tags: str | None = Form(None),
    skip_tags: str | None = Form(None),
    timeout_seconds: int = Form(3600),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    try:
        _safe_relative_path(playbook_path, settings.ansible_playbooks_dir)
        inventory_vars = _json_field(inventory_vars_json, "Inventory variables")
        extra_vars = _json_field(extra_vars_json, "Extra variables")
        config = profile.post_install_config
        if not config:
            config = PostInstallConfig(profile=profile, playbook_path=playbook_path)
            db.add(config)
        config.enabled = enabled == "on"
        config.playbook_path = playbook_path
        config.ssh_user = ssh_user
        config.ssh_port = ssh_port
        config.ssh_private_key_path = ssh_private_key_path or None
        config.become = become == "on"
        config.host_key_checking = host_key_checking == "on"
        config.inventory_groups = inventory_groups or None
        config.inventory_vars = inventory_vars
        config.extra_vars = extra_vars
        config.tags = tags or None
        config.skip_tags = skip_tags or None
        config.timeout_seconds = timeout_seconds
        db.commit()
    except (HTTPException, AnsibleRunError, ValueError) as exc:
        db.rollback()
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return templates.TemplateResponse(
            "post_install.html",
            context(
                request,
                user,
                profile=profile,
                config=profile.post_install_config,
                inventory_vars_json=inventory_vars_json or "{}",
                extra_vars_json=extra_vars_json or "{}",
                error=detail,
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(f"/profiles/{profile_id}/post-install", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/ansible-runs")
def ansible_runs_page(
    request: Request,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_or_redirect),
):
    runs = db.scalars(
        select(AnsibleRun).order_by(AnsibleRun.queued_at.desc(), AnsibleRun.id.desc()).limit(min(limit, 1000))
    ).all()
    return templates.TemplateResponse("ansible_runs.html", context(request, user, runs=runs))


@router.get("/assets")
def assets_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_or_redirect),
):
    assets = db.scalars(select(Asset).order_by(Asset.created_at.desc())).all()
    hosts = db.scalars(select(Host).order_by(Host.mac)).all()
    return templates.TemplateResponse(
        "assets.html",
        context(request, user, assets=assets, hosts=hosts, statuses=list(AssetStatus), error=None),
    )


@router.post("/assets")
def create_asset_form(
    request: Request,
    host_id: str | None = Form(None),
    asset_tag: str | None = Form(None),
    serial_number: str | None = Form(None),
    status: AssetStatus = Form(AssetStatus.PLANNED),
    owner: str | None = Form(None),
    department: str | None = Form(None),
    location: str | None = Form(None),
    manufacturer: str | None = Form(None),
    model: str | None = Form(None),
    notes: str | None = Form(None),
    metadata_json: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    try:
        parsed_host_id = _optional_int(host_id)
        if parsed_host_id and not db.get(Host, parsed_host_id):
            raise HTTPException(status_code=400, detail="Host does not exist")
        asset = Asset(
            host_id=parsed_host_id,
            asset_tag=asset_tag or None,
            serial_number=serial_number or None,
            status=status,
            owner=owner or None,
            department=department or None,
            location=location or None,
            manufacturer=manufacturer or None,
            model=model or None,
            notes=notes or None,
            metadata_json=_json_field(metadata_json, "Asset metadata"),
        )
        db.add(asset)
        db.commit()
    except (IntegrityError, HTTPException, ValueError) as exc:
        db.rollback()
        assets = db.scalars(select(Asset).order_by(Asset.created_at.desc())).all()
        hosts = db.scalars(select(Host).order_by(Host.mac)).all()
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return templates.TemplateResponse(
            "assets.html",
            context(request, user, assets=assets, hosts=hosts, statuses=list(AssetStatus), error=detail),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/assets", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/assets/{asset_id}")
def asset_detail_page(
    asset_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_or_redirect),
):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    hosts = db.scalars(select(Host).order_by(Host.mac)).all()
    builds = db.scalars(
        select(BuildRecord)
        .where(BuildRecord.asset_id == asset.id)
        .order_by(BuildRecord.started_at.desc(), BuildRecord.id.desc())
        .limit(50)
    ).all()
    return templates.TemplateResponse(
        "asset_detail.html",
        context(
            request,
            user,
            asset=asset,
            hosts=hosts,
            builds=builds,
            statuses=list(AssetStatus),
            metadata_json=json.dumps(asset.metadata_json or {}, indent=2),
            error=None,
        ),
    )


@router.post("/assets/{asset_id}")
def update_asset_form(
    asset_id: int,
    request: Request,
    host_id: str | None = Form(None),
    asset_tag: str | None = Form(None),
    serial_number: str | None = Form(None),
    status: AssetStatus = Form(...),
    owner: str | None = Form(None),
    department: str | None = Form(None),
    location: str | None = Form(None),
    manufacturer: str | None = Form(None),
    model: str | None = Form(None),
    notes: str | None = Form(None),
    metadata_json: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_csrf),
):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    try:
        parsed_host_id = _optional_int(host_id)
        if parsed_host_id and not db.get(Host, parsed_host_id):
            raise HTTPException(status_code=400, detail="Host does not exist")
        asset.host_id = parsed_host_id
        asset.asset_tag = asset_tag or None
        asset.serial_number = serial_number or None
        asset.status = status
        asset.owner = owner or None
        asset.department = department or None
        asset.location = location or None
        asset.manufacturer = manufacturer or None
        asset.model = model or None
        asset.notes = notes or None
        asset.metadata_json = _json_field(metadata_json, "Asset metadata")
        db.commit()
    except (IntegrityError, HTTPException, ValueError) as exc:
        db.rollback()
        hosts = db.scalars(select(Host).order_by(Host.mac)).all()
        builds = db.scalars(
            select(BuildRecord)
            .where(BuildRecord.asset_id == asset.id)
            .order_by(BuildRecord.started_at.desc(), BuildRecord.id.desc())
            .limit(50)
        ).all()
        if isinstance(exc, IntegrityError):
            detail = "Asset tag, serial number, or host is already used"
            status_code = status.HTTP_409_CONFLICT
        else:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            status_code = status.HTTP_400_BAD_REQUEST
        return templates.TemplateResponse(
            "asset_detail.html",
            context(
                request,
                user,
                asset=asset,
                hosts=hosts,
                builds=builds,
                statuses=list(AssetStatus),
                metadata_json=metadata_json or "{}",
                error=detail,
            ),
            status_code=status_code,
        )
    return RedirectResponse(f"/assets/{asset.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/events")
def events_page(
    request: Request,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_or_redirect),
):
    events = db.scalars(
        select(BootEvent).order_by(BootEvent.created_at.desc()).limit(min(limit, 1000))
    ).all()
    return templates.TemplateResponse("events.html", context(request, user, events=events))
