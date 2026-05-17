from __future__ import annotations

from pathlib import Path

import pytest

from app.models import AnsibleRun, Host, PostInstallConfig
from app.services.ansible_runner import AnsibleRunError, _safe_relative_path, build_environment, render_inventory


def test_safe_relative_path_rejects_traversal(tmp_path: Path):
    with pytest.raises(AnsibleRunError):
        _safe_relative_path("../outside.yml", tmp_path)


def test_safe_relative_path_allows_playbook_under_base(tmp_path: Path):
    playbook = _safe_relative_path("site.yml", tmp_path)

    assert playbook == (tmp_path / "site.yml").resolve()


def test_render_inventory_uses_host_ansible_variables():
    host = Host(
        mac="aa:bb:cc:dd:ee:ff",
        hostname="desk001",
        variables={"ansible_host": "192.0.2.55", "ansible": {"ansible_connection": "ssh"}},
    )
    config = PostInstallConfig(
        profile_id=1,
        enabled=True,
        playbook_path="site.yml",
        ssh_user="ubuntu",
        ssh_port=2222,
        ssh_private_key_path="/app/data/keys/id_ed25519",
        inventory_vars={"custom_var": "custom value"},
    )

    inventory = render_inventory(host, config)

    assert "[pxe_app]" in inventory
    assert "desk001" in inventory
    assert "ansible_host=192.0.2.55" in inventory
    assert "ansible_user=ubuntu" in inventory
    assert "ansible_port=2222" in inventory
    assert "custom_var='custom value'" in inventory
    assert "ansible_connection=ssh" in inventory


def test_build_environment_uses_writable_run_directories():
    run = AnsibleRun(id=123, host_id=1, playbook_path="site.yml")
    config = PostInstallConfig(profile_id=1, playbook_path="site.yml", host_key_checking=False)

    env = build_environment(run, config)

    assert env["HOME"].endswith("data/ansible-runs/123/home")
    assert env["ANSIBLE_LOCAL_TEMP"].endswith("data/ansible-runs/123/tmp")
    assert env["ANSIBLE_HOST_KEY_CHECKING"] == "False"
