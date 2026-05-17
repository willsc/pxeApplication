from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db, require_csrf
from app.models import AnsibleRun, Host, PostInstallConfig, Profile, User
from app.schemas import AnsibleRunOut, PostInstallConfigCreate, PostInstallConfigOut, PostInstallConfigUpdate
from app.services.ansible_runner import (
    AnsibleRunError,
    queue_ansible_run,
    retry_ansible_run,
    run_ansible_run,
    _safe_relative_path,
)
from app.settings import settings


router = APIRouter(tags=["post-install"])


def _validate_playbook(playbook_path: str) -> None:
    try:
        _safe_relative_path(playbook_path, settings.ansible_playbooks_dir)
    except AnsibleRunError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/profiles/{profile_id}/post-install", response_model=PostInstallConfigOut | None)
def get_post_install_config(
    profile_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    if not db.get(Profile, profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    return db.scalar(select(PostInstallConfig).where(PostInstallConfig.profile_id == profile_id))


@router.put("/api/profiles/{profile_id}/post-install", response_model=PostInstallConfigOut)
def upsert_post_install_config(
    profile_id: int,
    payload: PostInstallConfigCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    if payload.profile_id != profile_id:
        raise HTTPException(status_code=400, detail="profile_id must match URL")
    if not db.get(Profile, profile_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    _validate_playbook(payload.playbook_path)
    config = db.scalar(select(PostInstallConfig).where(PostInstallConfig.profile_id == profile_id))
    if config:
        for key, value in payload.model_dump().items():
            setattr(config, key, value)
    else:
        config = PostInstallConfig(**payload.model_dump())
        db.add(config)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Post-install config already exists") from exc
    db.refresh(config)
    return config


@router.patch("/api/profiles/{profile_id}/post-install", response_model=PostInstallConfigOut)
def update_post_install_config(
    profile_id: int,
    payload: PostInstallConfigUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    config = db.scalar(select(PostInstallConfig).where(PostInstallConfig.profile_id == profile_id))
    if not config:
        raise HTTPException(status_code=404, detail="Post-install config not found")
    values = payload.model_dump(exclude_unset=True)
    if "playbook_path" in values and values["playbook_path"]:
        _validate_playbook(values["playbook_path"])
    for key, value in values.items():
        setattr(config, key, value)
    db.commit()
    db.refresh(config)
    return config


@router.get("/api/ansible-runs", response_model=list[AnsibleRunOut])
def list_ansible_runs(
    host_id: int | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    stmt = select(AnsibleRun).order_by(AnsibleRun.queued_at.desc(), AnsibleRun.id.desc()).limit(min(limit, 500))
    if host_id:
        stmt = stmt.where(AnsibleRun.host_id == host_id)
    return db.scalars(stmt).all()


@router.get("/api/ansible-runs/{run_id}", response_model=AnsibleRunOut)
def get_ansible_run(
    run_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    run = db.get(AnsibleRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Ansible run not found")
    return run


@router.post("/api/hosts/{host_id}/run-ansible", response_model=AnsibleRunOut, status_code=status.HTTP_202_ACCEPTED)
def run_host_ansible(
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
        db.refresh(run)
        return run
    except AnsibleRunError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/ansible-runs/{run_id}/retry", response_model=AnsibleRunOut, status_code=status.HTTP_202_ACCEPTED)
def retry_run(
    run_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: User = Depends(require_csrf),
):
    previous = db.get(AnsibleRun, run_id)
    if not previous:
        raise HTTPException(status_code=404, detail="Ansible run not found")
    try:
        run = retry_ansible_run(db, previous)
        db.commit()
        background_tasks.add_task(run_ansible_run, run.id)
        db.refresh(run)
        return run
    except AnsibleRunError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

