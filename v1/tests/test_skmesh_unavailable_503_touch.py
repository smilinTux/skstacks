"""Behaviour of the skfenceha touch in the skmesh playbooks, under real
ansible-playbook: the tasks are lifted out of deploy_skmesh-<env>.yml as they
are and run against a temp root, with one local manager and one unreachable
one (192.0.2.1, TEST-NET-1, short ConnectTimeout).

* A manager that is down must not stop the play: the stack deploy comes after
  the touch, and redeploying skmesh to recover while a manager is down must
  still deploy the stack. The post-deploy warning names the missed manager.
* The touch runs every time the skfenceha dynamic dir exists, not only when
  the template changed: a run that stopped after the template step would
  otherwise leave the other managers without the fallback for good.
"""
import os
import pathlib
import shutil
import subprocess

import pytest
import yaml

SKMESH = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmesh"
STANDIN = "STAND-IN: stack deploy reached"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _lift(envname, root):
    play = yaml.safe_load((SKMESH / f"deploy_skmesh-{envname}.yml").read_text())
    tasks = [t for p in play if isinstance(p, dict) for t in p.get("tasks", [])]
    pick = lambda pred: next(t for t in tasks if pred(t))  # noqa: E731
    stat = pick(lambda t: "stat" in t and "dynamic" in t["stat"]["path"])
    tpl = pick(lambda t: "template" in t and "traefik-unavailable" in t["template"]["src"])
    touch = pick(lambda t: t.get("file", {}).get("state") == "touch")
    warn = pick(lambda t: "not touched" in t.get("name", ""))
    out = []
    for t in (stat, tpl, touch, {"name": "stand-in for the stack deploy", "debug": {"msg": STANDIN}}, warn):
        text = yaml.safe_dump(t).replace("/var/data/", f"{root}/var/data/") \
                                .replace("{{ playbook_dir }}", str(SKMESH))
        t = yaml.safe_load(text)
        t.get("template", {}).pop("owner", None)
        t.get("template", {}).pop("group", None)
        out.append(t)
    return out


def _run(tmp, envname):
    root = tmp / "root"
    fallback = root / f"var/data/config/skfenceha-{envname}/dynamic/skmesh-{envname}.yml"
    inv = tmp / "inventory.ini"
    inv.write_text(
        "[swarm_managers]\n"
        "manager-local ansible_connection=local\n"
        "manager-down ansible_host=192.0.2.1 ansible_connection=ssh ansible_user=nobody "
        "ansible_ssh_common_args='-o ConnectTimeout=2 -o BatchMode=yes -o StrictHostKeyChecking=no'\n")
    pb = tmp / "pb.yml"
    pb.write_text(yaml.safe_dump([{
        "hosts": "manager-local", "gather_facts": False,
        "vars": {"app": "skmesh", "env": envname,
                 "skmesh": {"CLUSTERNAME": "demo", "DOMAIN": "example.test"}},
        "tasks": _lift(envname, root)}]))
    env = {**os.environ, "ANSIBLE_HOST_KEY_CHECKING": "False", "ANSIBLE_NOCOLOR": "1",
           "ANSIBLE_RETRY_FILES_ENABLED": "False", "ANSIBLE_TIMEOUT": "3"}
    proc = subprocess.run(["ansible-playbook", "-i", str(inv), str(pb)], env=env,
                          capture_output=True, text=True, timeout=180)
    return proc, fallback


@pytest.mark.parametrize("envname", ["prod", "dev"])
def test_an_unreachable_manager_does_not_stop_the_play_and_is_reported(tmp_path, envname):
    (tmp_path / f"root/var/data/config/skfenceha-{envname}/dynamic").mkdir(parents=True)
    proc, fallback = _run(tmp_path, envname)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert fallback.is_file()
    assert STANDIN in out, out
    warning = next(l for l in out.splitlines() if "WARNING: the skmesh 503 fallback" in l)
    assert "manager-down" in warning and "manager-local" not in warning
    assert f"touch {tmp_path}/root/var/data/config/skfenceha-{envname}/dynamic/skmesh-{envname}.yml" in out


def test_a_second_run_with_an_unchanged_template_still_touches(tmp_path):
    (tmp_path / "root/var/data/config/skfenceha-prod/dynamic").mkdir(parents=True)
    proc, fallback = _run(tmp_path, "prod")
    assert proc.returncode == 0, proc.stdout
    os.utime(fallback, (1_000_000_000, 1_000_000_000))
    proc, _ = _run(tmp_path, "prod")
    assert proc.returncode == 0, proc.stdout
    assert fallback.stat().st_mtime > 1_000_000_000, "second run did not touch the fallback"
    # the template did not change and the touch reports unchanged: a clean run
    recap = next(l for l in proc.stdout.splitlines() if l.startswith("manager-local"))
    assert "changed=0" in recap, recap


def test_no_skfenceha_dir_means_no_touch_and_no_warning(tmp_path):
    (tmp_path / "root/var/data/config/skfence-prod/dynamic").mkdir(parents=True)
    proc, _ = _run(tmp_path, "prod")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert (tmp_path / "root/var/data/config/skfence-prod/dynamic/skmesh-prod.yml").is_file()
    assert STANDIN in out and "WARNING: the skmesh 503 fallback" not in out
    assert "192.0.2.1" not in out
