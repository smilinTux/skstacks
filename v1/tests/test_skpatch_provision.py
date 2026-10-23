"""provision_skpatch_nodes.yml rendered through REAL Ansible, same harness
machinery as render/render_playbooks.py (user/group/authorized_key/file
tasks are stubbed, never touching this test host's real accounts; the
sudoers template IS rendered for real, so its exact content is checked).
Covers: the per-host actions ceiling (vault ALLOW_ACTIONS vs a host's own
skpatch_allow_actions, the host var always wins), the missing/malformed
public key guard, and present/absent are both checked."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "skpatch"
PB = ROLE / "provision_skpatch_nodes.yml"
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexampleexampleexampleexampleexampleexamplekey example@render"


def run(tmp: Path, vault: dict | None, extra_vars: dict | None = None) -> dict:
    work = tmp / "work"
    shutil.copytree(rp.ANSIBLE, work / "ansible")
    for task_file in (work / "ansible").glob("**/tasks/*.yml"):
        if task_file.name != "select_vault_file.yml":
            rp.rewrite_task_file(task_file)

    run_dir = tmp / "run"
    vault_dir = run_dir / "vaults" / "optional" / "group_vars" / "prod"
    vault_dir.mkdir(parents=True)
    doc = {} if vault is None else {"skpatch": vault}
    (vault_dir / "skpatch-prod_vault.yml").write_text(yaml.safe_dump(doc))

    inv_dir = run_dir / "inventory"
    shutil.copytree(rp.FIXTURES, inv_dir)
    rp.write_inventory(inv_dir / "hosts.yml", "swarm_managers")
    # extra per-host vars (skpatch_allow_actions) merge onto localhost.
    inv = yaml.safe_load((inv_dir / "hosts.yml").read_text())
    inv["all"]["hosts"]["localhost"].update((extra_vars or {}))
    (inv_dir / "hosts.yml").write_text(yaml.safe_dump(inv))

    outdir = run_dir / "out"
    (outdir / "_shell").mkdir(parents=True)
    render_pb = work / "ansible" / "optional" / "skpatch" / "_render_provision.yml"
    rp.build_render_playbook(work / "ansible" / "optional" / "skpatch" / "provision_skpatch_nodes.yml", render_pb)

    env_vars = dict(__import__("os").environ,
                     ANSIBLE_LOCAL_TEMP=str(run_dir / ".ansible_tmp"),
                     ANSIBLE_HOST_KEY_CHECKING="False", ANSIBLE_RETRY_FILES_ENABLED="False",
                     ANSIBLE_DEPRECATION_WARNINGS="False", ANSIBLE_NOCOLOR="1",
                     ANSIBLE_GATHERING="smart")
    cmd = ["ansible-playbook", "-i", str(inv_dir / "hosts.yml"),
           "-e", f"render_outdir={outdir}",
           "-e", f"skstacks_vault_dir={run_dir / 'vaults'}",
           str(render_pb)]
    proc = subprocess.run(cmd, cwd=render_pb.parent, env=env_vars,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    res = {"rc": proc.returncode, "log": proc.stdout}
    if proc.returncode == 0:
        rp.unpack(outdir)
        bundle = outdir / "_render_files.json"
        res["files"] = {} if not bundle.exists() else None
    return res


def sudoers(tmp, vault, extra_vars=None) -> str:
    res = run(tmp, vault, extra_vars)
    assert res["rc"] == 0, res["log"][-4000:]
    return (tmp / "run/out/etc/sudoers.d/skpatch").read_text()


def fails(tmp, vault, extra_vars=None) -> str:
    res = run(tmp, vault, extra_vars)
    assert res["rc"] != 0, "the playbook rendered although it must refuse this vault"
    return res["log"]


# --- happy path --------------------------------------------------------

def test_present_with_global_allow_actions_false_writes_read_only_rule(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": False})
    assert "apt list --upgradable" in text
    assert "apt-get -s dist-upgrade" in text
    assert "reboot-required" in text
    assert "apt-get -y upgrade" not in text
    assert "/usr/sbin/reboot" not in text
    assert "skpatch-ro ALL=(root) NOPASSWD:" in text


def test_present_with_global_allow_actions_true_adds_the_action_lines(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True})
    assert "apt-get -y upgrade" in text
    assert "apt-get -y dist-upgrade" in text
    assert "apt-get -y autoremove" in text
    assert "/usr/sbin/reboot" in text


def test_custom_provision_user_name_is_used_in_the_rule(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "PROVISION_USER": "skpatch-nam"})
    assert "skpatch-nam ALL=(root) NOPASSWD:" in text
    assert "skpatch-ro" not in text


# --- the per-host ceiling always wins -----------------------------------

def test_host_level_false_overrides_global_true(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True},
                   extra_vars={"skpatch_allow_actions": False})
    assert "apt-get -y upgrade" not in text
    assert "/usr/sbin/reboot" not in text


def test_host_level_true_overrides_global_false(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": False},
                   extra_vars={"skpatch_allow_actions": True})
    assert "apt-get -y upgrade" in text
    assert "/usr/sbin/reboot" in text


def test_unset_host_level_falls_back_to_the_global_default(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY})  # ALLOW_ACTIONS unset -> false
    assert "apt-get -y upgrade" not in text


# --- failures -------------------------------------------------------------

@pytest.mark.parametrize("vault", [
    None,
    {},
    {"SSH_PUBLIC_KEY": ""},
    {"SSH_PUBLIC_KEY": "not-a-real-key AAAA"},
    {"SSH_PUBLIC_KEY": "rsa-sha2-512 AAAA"},
], ids=["no-block", "missing", "empty", "garbage", "wrong-prefix"])
def test_missing_or_malformed_public_key_fails_loudly(tmp_path, vault):
    log = fails(tmp_path, vault)
    assert "skpatch.SSH_PUBLIC_KEY is required" in log


def test_valid_key_types_are_each_accepted(tmp_path):
    for prefix in ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256"):
        sub = tmp_path / prefix
        sub.mkdir()
        sudoers(sub, {"SSH_PUBLIC_KEY": f"{prefix} AAAAexamplekeyexamplekeyexample example@render"})


# --- static -----------------------------------------------------------------

def test_absent_state_never_requires_a_public_key():
    pb_text = PB.read_text()
    assert "when: skpatch_state == 'present'" in pb_text
    # the pubkey guard is explicitly gated to state == present
    guard = pb_text.split("Fail loudly when the SSH public key is missing")[1].split("- name:")[0]
    assert "when: skpatch_state == 'present'" in guard


def test_absent_removes_the_account_key_and_sudoers_rule():
    pb_text = PB.read_text()
    assert "state: absent" in pb_text
    assert "remove: true" in pb_text
    assert "/etc/sudoers.d/skpatch" in pb_text
