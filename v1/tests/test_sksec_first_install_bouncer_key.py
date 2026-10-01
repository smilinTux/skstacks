"""sksec's bouncer key on a FIRST install (no vault key, no persisted file).

The deploy stats `secret/bouncer.key`, generates it when missing, then reads
it back to compute the key. The read was gated on the stat taken BEFORE the
generation (`when: bouncer_key_file.stat.exists`), so on a fresh install the
freshly generated key was never read, `computed_bouncer_key` was empty and the
deploy stopped with "CROWDSEC_BOUNCER_API_KEY could not be determined": the
generate-and-persist path #92 documents could never succeed on its first run
(found by the skstack06 v2.22.0 run, sksec after skfenceha).

This runs the playbook's own key tasks, unchanged except for paths (a scratch
dir instead of /var/data/config) and the root owner (tests do not run as
root), through real `ansible-playbook` on localhost, for every env.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOKS = {e: ROOT / f"v1/ansible/core/sksec/deploy_sksec-{e}.yml" for e in ("dev", "staging", "prod")}
FIRST = "Ensure secret directory exists"
LAST = "Fail if bouncer key still missing (safety)"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def key_tasks(env):
    plays = yaml.safe_load(PLAYBOOKS[env].read_text())
    for play in plays:
        names = [t.get("name") for t in play.get("tasks", [])]
        if FIRST in names and LAST in names:
            return play["tasks"][names.index(FIRST): names.index(LAST) + 1]
    raise AssertionError(f"{env}: key tasks not found")


def relocate(obj, base):
    if isinstance(obj, str):
        return obj.replace("/var/data/config/{{ app }}-{{ env }}", str(base))
    if isinstance(obj, list):
        return [relocate(x, base) for x in obj]
    if isinstance(obj, dict):
        return {k: relocate(v, base) for k, v in obj.items() if k not in ("owner", "group")}
    return obj


def run(tmp_path, env, vault_key=None):
    base = tmp_path / "cfg"
    tasks = relocate(key_tasks(env), base)
    out = tmp_path / "computed.json"
    tasks.append({"name": "record", "copy": {"dest": str(out), "content": "{{ {'key': computed_bouncer_key} | to_json }}"}})
    sksec = {"CROWDSEC_BOUNCER_API_KEY": vault_key} if vault_key is not None else {}
    play = [{"hosts": "localhost", "gather_facts": False, "connection": "local",
             "vars": {"app": "sksec", "env": env, "sksec": sksec}, "tasks": tasks}]
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump(play, sort_keys=False))
    env_vars = dict(os.environ, HOME=str(tmp_path), ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="False",
                    ANSIBLE_INVENTORY_UNPARSED_WARNING="False")
    r = subprocess.run(["ansible-playbook", "-i", "localhost,", str(pb)], capture_output=True, text=True, env=env_vars)
    key = json.loads(out.read_text())["key"] if out.exists() else None
    return r, key, base / "secret" / "bouncer.key"


@pytest.mark.parametrize("env", list(PLAYBOOKS))
def test_first_install_generates_persists_and_uses_the_key(tmp_path, env):
    r, key, f = run(tmp_path, env)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert f.is_file() and f.read_text().strip()
    assert key == f.read_text().strip()
    assert oct(f.stat().st_mode & 0o777) == "0o640"


@pytest.mark.parametrize("env", list(PLAYBOOKS))
def test_redeploy_keeps_the_persisted_key(tmp_path, env):
    r1, k1, f = run(tmp_path, env)
    assert r1.returncode == 0, r1.stdout[-2000:]
    r2, k2, _ = run(tmp_path, env)
    assert r2.returncode == 0, r2.stdout[-2000:]
    assert k1 and k1 == k2 == f.read_text().strip()


@pytest.mark.parametrize("env", list(PLAYBOOKS))
def test_vault_key_wins_and_nothing_is_generated(tmp_path, env):
    r, key, f = run(tmp_path, env, vault_key="vault-provided-key")
    assert r.returncode == 0, r.stdout[-2000:]
    assert key == "vault-provided-key"
    assert not f.exists()


@pytest.mark.parametrize("env", list(PLAYBOOKS))
def test_vault_key_wins_over_a_persisted_file(tmp_path, env):
    run(tmp_path, env)
    r, key, f = run(tmp_path, env, vault_key="vault-provided-key")
    assert r.returncode == 0, r.stdout[-2000:]
    assert key == "vault-provided-key"
