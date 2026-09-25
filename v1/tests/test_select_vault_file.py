"""select_vault_file.yml: where a service's vault is looked up.

Runs the real task file with ansible-playbook from a throwaway service dir
under v1/ansible/optional/, because the task derives paths from playbook_dir.
"""
import pathlib
import shutil
import subprocess

V1 = pathlib.Path(__file__).resolve().parents[1]
PROBE = """
- hosts: localhost
  gather_facts: false
  vars: {env: dev}
  tasks:
    - import_tasks: ../../shared/tasks/select_vault_file.yml
    - debug: {msg: "VAULT={{ vault_file_path | trim }}"}
"""


def _vault_path(tmp_path, *extra):
    svc = V1 / "ansible" / "optional" / "zzprobe"
    svc.mkdir()
    try:
        (svc / "probe.yml").write_text(PROBE)
        inv = tmp_path / "inv.ini"
        inv.write_text("localhost ansible_connection=local domain=example.test cluster_name=c1\n")
        out = subprocess.run(
            ["ansible-playbook", "-i", str(inv), str(svc / "probe.yml"), *extra],
            capture_output=True, text=True, check=True,
        ).stdout
    finally:
        shutil.rmtree(svc)
    line = next(l for l in out.splitlines() if "VAULT=" in l)
    return line.split("VAULT=", 1)[1].strip().strip('"')


def test_default_resolves_beside_the_playbooks(tmp_path):
    assert _vault_path(tmp_path) == str(V1 / "ansible/optional/group_vars/dev/zzprobe-dev_vault.yml")


def test_vault_dir_moves_the_lookup_into_the_instance(tmp_path):
    inst = tmp_path / "inst"
    got = _vault_path(tmp_path, "-e", f"skstacks_vault_dir={inst}")
    assert got == f"{inst}/optional/group_vars/dev/zzprobe-dev_vault.yml"


def test_vault_dir_prefers_the_domain_cluster_file_when_present(tmp_path):
    inst = tmp_path / "inst"
    f = inst / "optional/group_vars/dev/zzprobe-dev-example-test-c1_vault.yml"
    f.parent.mkdir(parents=True)
    f.write_text("x: 1\n")
    assert _vault_path(tmp_path, "-e", f"skstacks_vault_dir={inst}") == str(f)
