"""shared/tasks/nfs_resvport_guard.yml keeps the kernel NFS/RPC client off
host-mode ports a service publishes (2026-11-25: an NFS socket on a manager
took 993 and traefik-worker could not bind IMAPS there).

The task file runs under ansible-playbook on localhost in check mode, so
nothing is written: --diff shows what would land in /etc/modprobe.d and
/etc/sysctl.d. The skfenceha playbooks must include it for every manager,
with the mail ports only when MAIL_ENTRYPOINTS is on.
"""
import pathlib
import shutil
import subprocess

import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
GUARD = ANSIBLE / "shared/tasks/nfs_resvport_guard.yml"
needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


def _run(tmp_path, ports):
    play = tmp_path / "play.yml"
    play.write_text(yaml.safe_dump([{
        "hosts": "localhost",
        "gather_facts": False,
        "vars": {"nfs_resvport_guard_hosts": ["localhost"], "nfs_resvport_guard_ports": ports},
        "tasks": [{"include_tasks": str(GUARD)}],
    }]))
    return subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", "--check", "--diff", str(play)],
        capture_output=True, text=True, timeout=180,
        env={**__import__("os").environ, "ANSIBLE_NOCOLOR": "1", "ANSIBLE_RETRY_FILES_ENABLED": "0"},
    )


@needs_ansible
def test_no_port_in_rpc_range_changes_nothing(tmp_path):
    proc = _run(tmp_path, [80, 443, 222, 8082])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "max_resvport" not in proc.stdout
    assert "Write the sunrpc module option" in proc.stdout  # listed, but skipped
    assert "changed=0" in proc.stdout


@needs_ansible
def test_imaps_lowers_the_ceiling_to_992_in_both_files(tmp_path):
    proc = _run(tmp_path, [80, 443, 25, 465, 587, 143, 993])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "+options sunrpc max_resvport=992" in proc.stdout
    assert "+sunrpc.max_resvport = 992" in proc.stdout
    assert "/etc/modprobe.d/sunrpc.conf" in proc.stdout
    assert "/etc/sysctl.d/96-sunrpc-resvport.conf" in proc.stdout


@needs_ansible
def test_lowest_guarded_port_in_range_wins(tmp_path):
    proc = _run(tmp_path, ["993", 900])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "+options sunrpc max_resvport=899" in proc.stdout


@needs_ansible
def test_a_port_that_starves_the_rpc_client_is_refused(tmp_path):
    proc = _run(tmp_path, [700])
    assert proc.returncode != 0
    assert "would leave sunrpc only 35 reserved ports" in proc.stdout


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_skfenceha_playbooks_guard_every_manager(env):
    plays = yaml.safe_load((ANSIBLE / f"core/skfenceha/deploy_skfenceha-{env}.yml").read_text())
    tasks = [t for p in plays for t in p.get("tasks", [])]
    guard = [t for t in tasks if str(t.get("include_tasks", "")).endswith("shared/tasks/nfs_resvport_guard.yml")]
    assert len(guard) == 1
    v = guard[0]["vars"]
    assert v["nfs_resvport_guard_hosts"] == "{{ groups[target_manager_group] }}"
    assert "993" in v["nfs_resvport_guard_ports"] and "MAIL_ENTRYPOINTS" in v["nfs_resvport_guard_ports"]
