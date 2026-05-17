from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import AnsibleRunState, AssetStatus, BuildState, HostState, OSType
from app.services.ipxe import normalize_mac


class ImageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    os_type: OSType
    architecture: str = "x86_64"
    kernel_path: str | None = None
    initrd_path: str | None = None
    repo_url: str | None = None
    bootloader_path: str | None = None
    wim_path: str | None = None
    bcd_path: str | None = None
    boot_sdi_path: str | None = None
    extra_kernel_args: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class ImageUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    os_type: OSType | None = None
    architecture: str | None = None
    kernel_path: str | None = None
    initrd_path: str | None = None
    repo_url: str | None = None
    bootloader_path: str | None = None
    wim_path: str | None = None
    bcd_path: str | None = None
    boot_sdi_path: str | None = None
    extra_kernel_args: str | None = None
    metadata_json: dict[str, Any] | None = None


class ImageOut(ImageCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    image_id: int
    template_path: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    authorized_keys: str | None = None
    root_password: str | None = None


class ProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    image_id: int | None = None
    template_path: str | None = None
    variables: dict[str, Any] | None = None
    authorized_keys: str | None = None
    root_password: str | None = None


class ProfileOut(ProfileCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


class HostCreate(BaseModel):
    mac: str
    hostname: str | None = Field(default=None, max_length=255)
    profile_id: int | None = None
    variables: dict[str, Any] = Field(default_factory=dict)

    @field_validator("mac")
    @classmethod
    def validate_mac(cls, value: str) -> str:
        return normalize_mac(value)


class HostUpdate(BaseModel):
    hostname: str | None = Field(default=None, max_length=255)
    profile_id: int | None = None
    variables: dict[str, Any] | None = None
    state: HostState | None = None


class HostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mac: str
    hostname: str | None
    profile_id: int | None
    state: HostState
    variables: dict[str, Any]
    last_boot_at: datetime | None
    provisioned_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CallbackPayload(BaseModel):
    status: Literal["done", "success", "failed", "fail", "error"]
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class BootEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    host_id: int | None
    mac: str | None
    event_type: str
    source_ip: str | None
    user_agent: str | None
    path: str | None
    token_prefix: str | None
    payload: dict[str, Any]
    created_at: datetime


class AssetCreate(BaseModel):
    host_id: int | None = None
    asset_tag: str | None = Field(default=None, max_length=128)
    serial_number: str | None = Field(default=None, max_length=160)
    status: AssetStatus = AssetStatus.PLANNED
    owner: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=255)
    location: str | None = Field(default=None, max_length=255)
    manufacturer: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=255)
    notes: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class AssetUpdate(BaseModel):
    host_id: int | None = None
    asset_tag: str | None = Field(default=None, max_length=128)
    serial_number: str | None = Field(default=None, max_length=160)
    status: AssetStatus | None = None
    owner: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=255)
    location: str | None = Field(default=None, max_length=255)
    manufacturer: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=255)
    notes: str | None = None
    metadata_json: dict[str, Any] | None = None


class AssetOut(AssetCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


class BuildRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    host_id: int
    asset_id: int | None
    profile_id: int | None
    image_id: int | None
    state: BuildState
    token_prefix: str | None
    started_at: datetime
    completed_at: datetime | None
    metadata_json: dict[str, Any]


class PostInstallConfigCreate(BaseModel):
    profile_id: int
    enabled: bool = False
    playbook_path: str = Field(min_length=1, max_length=512)
    ssh_user: str = Field(default="root", max_length=128)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_private_key_path: str | None = Field(default=None, max_length=512)
    become: bool = True
    host_key_checking: bool = False
    inventory_groups: str | None = Field(default=None, max_length=512)
    inventory_vars: dict[str, Any] = Field(default_factory=dict)
    extra_vars: dict[str, Any] = Field(default_factory=dict)
    tags: str | None = Field(default=None, max_length=512)
    skip_tags: str | None = Field(default=None, max_length=512)
    timeout_seconds: int = Field(default=3600, ge=1, le=86400)


class PostInstallConfigUpdate(BaseModel):
    enabled: bool | None = None
    playbook_path: str | None = Field(default=None, min_length=1, max_length=512)
    ssh_user: str | None = Field(default=None, max_length=128)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_private_key_path: str | None = Field(default=None, max_length=512)
    become: bool | None = None
    host_key_checking: bool | None = None
    inventory_groups: str | None = Field(default=None, max_length=512)
    inventory_vars: dict[str, Any] | None = None
    extra_vars: dict[str, Any] | None = None
    tags: str | None = Field(default=None, max_length=512)
    skip_tags: str | None = Field(default=None, max_length=512)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)


class PostInstallConfigOut(PostInstallConfigCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


class AnsibleRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    host_id: int
    profile_id: int | None
    config_id: int | None
    build_record_id: int | None
    state: AnsibleRunState
    playbook_path: str
    inventory_path: str | None
    command: list[str]
    return_code: int | None
    stdout: str | None
    stderr: str | None
    error: str | None
    trigger: str
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    metadata_json: dict[str, Any]
