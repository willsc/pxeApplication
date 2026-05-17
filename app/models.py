from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HostState(str, enum.Enum):
    PENDING = "PENDING"
    READY = "READY"
    INSTALLING = "INSTALLING"
    PROVISIONED = "PROVISIONED"
    FAILED = "FAILED"
    DECOMMISSIONED = "DECOMMISSIONED"


class OSType(str, enum.Enum):
    RHEL = "rhel"
    UBUNTU = "ubuntu"
    DEBIAN = "debian"
    WINDOWS = "windows"


class AssetStatus(str, enum.Enum):
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    SPARE = "SPARE"
    REPAIR = "REPAIR"
    RETIRED = "RETIRED"
    LOST = "LOST"


class BuildState(str, enum.Enum):
    INSTALLING = "INSTALLING"
    PROVISIONED = "PROVISIONED"
    FAILED = "FAILED"


class AnsibleRunState(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"


class MediaImportState(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Image(Base):
    __tablename__ = "images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True, index=True, nullable=False)
    os_type: Mapped[OSType] = mapped_column(Enum(OSType), nullable=False)
    architecture: Mapped[str] = mapped_column(String(32), default="x86_64", nullable=False)
    kernel_path: Mapped[str | None] = mapped_column(String(512))
    initrd_path: Mapped[str | None] = mapped_column(String(512))
    repo_url: Mapped[str | None] = mapped_column(String(1024))
    bootloader_path: Mapped[str | None] = mapped_column(String(512))
    wim_path: Mapped[str | None] = mapped_column(String(512))
    bcd_path: Mapped[str | None] = mapped_column(String(512))
    boot_sdi_path: Mapped[str | None] = mapped_column(String(512))
    extra_kernel_args: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    profiles: Mapped[list["Profile"]] = relationship(back_populates="image")


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True, index=True, nullable=False)
    image_id: Mapped[int] = mapped_column(ForeignKey("images.id"), nullable=False)
    template_path: Mapped[str | None] = mapped_column(String(512))
    variables: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    authorized_keys: Mapped[str | None] = mapped_column(Text)
    root_password: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    image: Mapped[Image] = relationship(back_populates="profiles")
    hosts: Mapped[list["Host"]] = relationship(back_populates="profile")
    post_install_config: Mapped["PostInstallConfig | None"] = relationship(
        back_populates="profile",
        uselist=False,
        cascade="all, delete-orphan",
    )
    extras: Mapped["ProfileExtras | None"] = relationship(
        back_populates="profile",
        uselist=False,
        cascade="all, delete-orphan",
    )


class Host(Base):
    __tablename__ = "hosts"
    __table_args__ = (UniqueConstraint("mac", name="uq_hosts_mac"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mac: Mapped[str] = mapped_column(String(17), index=True, nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(255), index=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id"))
    state: Mapped[HostState] = mapped_column(Enum(HostState), default=HostState.PENDING, nullable=False)
    install_token: Mapped[str] = mapped_column(String(96), unique=True, index=True, nullable=False)
    variables: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    last_boot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    profile: Mapped[Profile | None] = relationship(back_populates="hosts")
    events: Mapped[list["BootEvent"]] = relationship(back_populates="host", cascade="all, delete-orphan")
    asset: Mapped["Asset | None"] = relationship(back_populates="host", uselist=False)
    build_records: Mapped[list["BuildRecord"]] = relationship(back_populates="host")
    ansible_runs: Mapped[list["AnsibleRun"]] = relationship(back_populates="host")


class BootEvent(Base):
    __tablename__ = "boot_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int | None] = mapped_column(ForeignKey("hosts.id"))
    mac: Mapped[str | None] = mapped_column(String(17), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source_ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    path: Mapped[str | None] = mapped_column(String(1024))
    token_prefix: Mapped[str | None] = mapped_column(String(12))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)

    host: Mapped[Host | None] = relationship(back_populates="events")


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int | None] = mapped_column(ForeignKey("hosts.id"), unique=True)
    asset_tag: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    serial_number: Mapped[str | None] = mapped_column(String(160), unique=True, index=True)
    status: Mapped[AssetStatus] = mapped_column(Enum(AssetStatus), default=AssetStatus.PLANNED, nullable=False)
    owner: Mapped[str | None] = mapped_column(String(255))
    department: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    manufacturer: Mapped[str | None] = mapped_column(String(255))
    model: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    host: Mapped[Host | None] = relationship(back_populates="asset")
    build_records: Mapped[list["BuildRecord"]] = relationship(back_populates="asset")


class BuildRecord(Base):
    __tablename__ = "build_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id"), index=True, nullable=False)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"), index=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id"))
    image_id: Mapped[int | None] = mapped_column(ForeignKey("images.id"))
    state: Mapped[BuildState] = mapped_column(Enum(BuildState), default=BuildState.INSTALLING, nullable=False)
    token_prefix: Mapped[str | None] = mapped_column(String(12))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    host: Mapped[Host] = relationship(back_populates="build_records")
    asset: Mapped[Asset | None] = relationship(back_populates="build_records")
    profile: Mapped[Profile | None] = relationship()
    image: Mapped[Image | None] = relationship()


class PostInstallConfig(Base):
    __tablename__ = "post_install_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    playbook_path: Mapped[str] = mapped_column(String(512), nullable=False)
    ssh_user: Mapped[str] = mapped_column(String(128), default="root", nullable=False)
    ssh_port: Mapped[int] = mapped_column(Integer, default=22, nullable=False)
    ssh_private_key_path: Mapped[str | None] = mapped_column(String(512))
    become: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    host_key_checking: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    inventory_groups: Mapped[str | None] = mapped_column(String(512))
    inventory_vars: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    extra_vars: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    tags: Mapped[str | None] = mapped_column(String(512))
    skip_tags: Mapped[str | None] = mapped_column(String(512))
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    profile: Mapped[Profile] = relationship(back_populates="post_install_config")
    runs: Mapped[list["AnsibleRun"]] = relationship(back_populates="config")


class AnsibleRun(Base):
    __tablename__ = "ansible_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id"), index=True, nullable=False)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id"), index=True)
    config_id: Mapped[int | None] = mapped_column(ForeignKey("post_install_configs.id"), index=True)
    build_record_id: Mapped[int | None] = mapped_column(ForeignKey("build_records.id"), index=True)
    state: Mapped[AnsibleRunState] = mapped_column(
        Enum(AnsibleRunState),
        default=AnsibleRunState.QUEUED,
        index=True,
        nullable=False,
    )
    playbook_path: Mapped[str] = mapped_column(String(512), nullable=False)
    inventory_path: Mapped[str | None] = mapped_column(String(1024))
    command: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    return_code: Mapped[int | None] = mapped_column(Integer)
    stdout: Mapped[str | None] = mapped_column(Text)
    stderr: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(String(64), default="callback", nullable=False)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    host: Mapped[Host] = relationship(back_populates="ansible_runs")
    profile: Mapped[Profile | None] = relationship()
    config: Mapped[PostInstallConfig | None] = relationship(back_populates="runs")
    build_record: Mapped[BuildRecord | None] = relationship()


class MediaImportRun(Base):
    __tablename__ = "media_import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    state: Mapped[MediaImportState] = mapped_column(
        Enum(MediaImportState),
        default=MediaImportState.QUEUED,
        index=True,
        nullable=False,
    )
    command: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    return_code: Mapped[int | None] = mapped_column(Integer)
    stdout: Mapped[str | None] = mapped_column(Text)
    stderr: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ProfileExtras(Base):
    """Customization slots injected at the END of an unattended install.

    These run while the installer is still in control (late-commands, %post,
    preseed late_command, FirstLogonCommands), so the install stays
    unattended. PostInstallConfig (separate table) covers the *after-boot*
    Ansible run reachable over SSH.
    """

    __tablename__ = "profile_extras"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"), unique=True, nullable=False)
    extra_packages: Mapped[str | None] = mapped_column(Text)
    finish_script_bash: Mapped[str | None] = mapped_column(Text)
    finish_script_powershell: Mapped[str | None] = mapped_column(Text)
    ansible_pull_url: Mapped[str | None] = mapped_column(String(1024))
    ansible_pull_playbook: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    profile: Mapped[Profile] = relationship(back_populates="extras")
