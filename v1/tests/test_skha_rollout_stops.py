"""skha restart handler: a node that is not ready stops the rollout.

Runs the real handler (deploy_skha-prod.yml + restart_keepalived.yml) with
ansible-playbook on three local hosts and a fake systemctl. throttle: 1 with
any_errors_fatal did NOT do this (the next managers still restarted), so the
handler is one run_once task looping over the changed managers in order."""
import os
import pathlib
import shutil
import subprocess

import pytest
import yaml

SKHA = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skha"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

FAKE_SYSTEMCTL = """#!/bin/bash
case "$1" in
  start)
    echo "$SKHA_STATE" >> "$(dirname "$SKHA_STATE")/starts.log"
    echo fault > "$SKHA_STATE"
    case "$SKHA_STATE" in *ready*) (sleep 1; echo backup > "$SKHA_STATE") & ;; esac ;;
  is-active) echo active ;;
esac
exit 0
"""


def handler(path):
    plays = yaml.safe_load(path.read_text())
    h = [t for p in plays for t in (p.get("handlers") or []) if t["name"] == "restart keepalived"][0]
    h["ansible.builtin.include_tasks"] = h["ansible.builtin.include_tasks"].replace("{{ playbook_dir }}", str(SKHA))
    return h


def run(tmp_path, nodes):
    """nodes: list of (name, ready, changed). Returns hosts in start order."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "systemctl").write_text(FAKE_SYSTEMCTL)
    (fakebin / "systemctl").chmod(0o755)
    inv = "\n".join(
        f"{n} ansible_connection=local ansible_python_interpreter={shutil.which('python3')} "
        f"skha_state_file={tmp_path}/{n}-{'ready' if r else 'stuck'}.state "
        f"skha_changed={'true' if c else 'false'}"
        for n, r, c in nodes
    )
    (tmp_path / "inv").write_text(inv + "\n")
    play = [{
        "hosts": "all",
        "gather_facts": False,
        "environment": {"PATH": f"{fakebin}:{os.environ['PATH']}"},
        "tasks": [
            {"set_fact": {"skha_restart_wait": 3, "skha_conf": {"changed": "{{ skha_changed | bool }}"}}},
            {"name": "config changed", "debug": {"msg": "x"}, "changed_when": "skha_changed | bool",
             "notify": "restart keepalived"},
        ],
        "handlers": [handler(SKHA / "deploy_skha-prod.yml")],
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    res = subprocess.run(["ansible-playbook", "-i", str(tmp_path / "inv"), str(tmp_path / "play.yml")],
                         capture_output=True, text=True, timeout=120, env={**os.environ, "ANSIBLE_NOCOLOR": "1"})
    log = tmp_path / "starts.log"
    starts = [pathlib.Path(l).name.split("-")[0] for l in log.read_text().split()] if log.exists() else []
    return res, starts


def test_all_ready_restarts_every_manager_in_order(tmp_path):
    res, starts = run(tmp_path, [("h1", True, True), ("h2", True, True), ("h3", True, True)])
    assert res.returncode == 0, res.stdout + res.stderr
    assert starts == ["h1", "h2", "h3"]


def test_stuck_node_stops_the_rollout(tmp_path):
    res, starts = run(tmp_path, [("h1", True, True), ("h2", False, True), ("h3", True, True)])
    assert res.returncode != 0
    assert starts == ["h1", "h2"], res.stdout
    assert "keepalived on h2 not MASTER/BACKUP" in res.stdout


def test_only_changed_managers_restart(tmp_path):
    res, starts = run(tmp_path, [("h1", True, True), ("h2", True, False), ("h3", True, True)])
    assert res.returncode == 0, res.stdout + res.stderr
    assert starts == ["h1", "h3"]
