from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PXE_", env_file=".env", extra="ignore")

    app_name: str = "pxe-app"
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://pxe:pxe-change-me@postgres:5432/pxeapp"
    secret_key: SecretStr = Field(default=SecretStr("dev-only-change-me"))
    listen_host: str = "0.0.0.0"
    listen_port: int = Field(default=8000, ge=1, le=65535)
    public_base_url: str = "http://127.0.0.1:8000"
    files_base_url: str = "http://127.0.0.1:8080"
    pxe_templates_dir: Path = Path("pxe_templates")
    ansible_playbooks_dir: Path = Path("ansible/playbooks")
    ansible_work_dir: Path = Path("data/ansible-runs")
    tftproot_dir: Path = Path("tftproot")
    dnsmasq_config_path: Path = Path("dnsmasq/dnsmasq.conf")
    dhcp_mode: Literal["auto", "proxy", "server"] | None = None
    pxe_network: str | None = None
    pxe_host_ip: str | None = None
    pxe_probe_interface: str | None = None
    pxe_probe_timeout: float = 5.0
    dhcp_probe_url: str | None = None
    unknown_host_policy: Literal["localboot", "menu", "register"] = "menu"
    unattended_auto_enroll: bool = True
    unattended_default_profile_name: str | None = None
    session_cookie_name: str = "pxe_session"
    session_cookie_secure: bool = False
    session_max_age_seconds: int = 12 * 60 * 60
    csrf_max_age_seconds: int = 12 * 60 * 60
    initial_admin_username: str | None = None
    initial_admin_password: SecretStr | None = None
    enable_openapi: bool = False

    @field_validator("public_base_url", "files_base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        secret = self.secret_key.get_secret_value()
        if self.environment == "production":
            if secret == "dev-only-change-me" or len(secret) < 32:
                raise ValueError("PXE_SECRET_KEY must be a unique value of at least 32 characters")
            if self.initial_admin_password and len(self.initial_admin_password.get_secret_value()) < 12:
                raise ValueError("PXE_INITIAL_ADMIN_PASSWORD must be at least 12 characters")
        return self


settings = Settings()
