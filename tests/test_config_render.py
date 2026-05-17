from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Host, HostState, Image, OSType, Profile
from app.services.config_render import ConfigRenderError, render_config, template_for_host
from app.settings import Settings


def test_template_path_rejects_traversal():
    image = Image(name="rocky", os_type=OSType.RHEL)
    profile = Profile(name="bad", image=image, template_path="../secret")
    host = Host(
        mac="aa:bb:cc:dd:ee:ff",
        profile=profile,
        state=HostState.READY,
        install_token="token123",
    )

    with pytest.raises(ConfigRenderError):
        template_for_host(host)


def test_rhel_config_render_includes_host_values():
    settings = Settings(
        environment="test",
        secret_key="x" * 40,
        public_base_url="http://pxe.test:8000",
        files_base_url="http://pxe.test:8080",
        pxe_templates_dir=Path("pxe_templates"),
    )
    image = Image(
        name="rocky",
        os_type=OSType.RHEL,
        repo_url="http://mirror/rocky",
    )
    profile = Profile(
        name="linux",
        image=image,
        variables={"timezone": "Europe/London"},
        authorized_keys="ssh-ed25519 AAAATEST operator@example",
        root_password="VeryLongPassword123!",
    )
    host = Host(
        mac="aa:bb:cc:dd:ee:ff",
        hostname="desk001",
        profile=profile,
        state=HostState.READY,
        install_token="token123",
        variables={"autopart_type": "lvm"},
    )

    body, media_type = render_config(host, settings)

    assert media_type == "text/plain"
    assert "network --bootproto=dhcp --device=link --activate --hostname=desk001" in body
    assert "timezone Europe/London --utc" in body
    assert "ssh-ed25519 AAAATEST operator@example" in body
    assert "http://pxe.test:8000/api/boot/callback/token123" in body
    assert "VeryLongPassword123!" not in body

