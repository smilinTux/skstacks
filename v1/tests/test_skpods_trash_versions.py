"""skpods: no Nextcloud trash and no version history.

A Solid DELETE means the resource is gone. With files_trashbin on, Nextcloud
copies every deleted file into the account's local files_trashbin, also when
the pod is an external mount (S3, later WebDAV), so the file stays on this
server after the user deleted it from the storage they chose (seen on
pods-dev, 2026-11-27). files_versions keeps old versions in the local
files_versions the same way. The role therefore disables both by default and
empties every trash before it disables files_trashbin.

The three app tasks of tasks/engine_nextcloud.yml run here through REAL
Ansible and bash, against a fake `docker` that plays occ and records calls.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

TASKS = Path(__file__).resolve().parents[1] / "ansible" / "optional" / "skpods" / "tasks" / "engine_nextcloud.yml"
NAMES = (
    "Work out which apps to disable and which to enable",
    "Disable the default apps this server does not need",
    "Enable the Files features skpods uses",
)

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
state_file = os.environ["FAKE_OCC_STATE"]
state = json.load(open(state_file))
args = sys.argv[1:]
occ = args[args.index("occ") + 1:] if "occ" in args else []
with open(os.environ["FAKE_OCC_LOG"], "a") as log:
    log.write(" ".join(occ) + "\n")
if occ[:2] == ["app:list", "--enabled"]:
    print(json.dumps({"enabled": {a: "1.0" for a in state["enabled"]}}))
elif occ[:1] == ["app:disable"]:
    state["enabled"] = [a for a in state["enabled"] if a != occ[1]]
elif occ[:1] == ["app:enable"]:
    state["enabled"].append(occ[1])
elif occ[:1] == ["trashbin:cleanup"]:
    if os.environ.get("FAKE_CLEANUP_FAILS"):
        sys.exit(1)
json.dump(state, open(state_file, "w"))
"""


def tasks():
    by_name = {t["name"]: t for t in yaml.safe_load(TASKS.read_text())}
    return [by_name[n] for n in NAMES]


def run(tmp: Path, enabled: list[str], skpods: dict | None = None, cleanup_fails: bool = False):
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)
    state, log = tmp / "state.json", tmp / "occ.log"
    state.write_text(json.dumps({"enabled": enabled}))
    log.write_text("")
    play = [{
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": {
            "skpods": skpods or {},
            "skp_nc_container": {"stdout": "fake-nc"},
            "skp_nc_node": {"stdout": "localhost"},
        },
        "tasks": tasks(),
    }]
    (tmp / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False, width=100000))
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_OCC_STATE=str(state),
               FAKE_OCC_LOG=str(log), ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="false",
               ANSIBLE_INVENTORY_UNPARSED_WARNING="false")
    if cleanup_fails:
        env["FAKE_CLEANUP_FAILS"] = "1"
    res = subprocess.run(["ansible-playbook", "-i", "localhost,", str(tmp / "play.yml")],
                         cwd=tmp, env=env, capture_output=True, text=True, timeout=300)
    calls = [line for line in log.read_text().splitlines() if line and not line.startswith("app:list")]
    return res, calls, json.loads(state.read_text())["enabled"]


def test_default_empties_trash_then_disables_trash_and_versions(tmp_path):
    res, calls, enabled = run(tmp_path, ["files", "files_trashbin", "files_versions", "text"])
    assert res.returncode == 0, res.stdout[-3000:]
    assert "files_trashbin" not in enabled and "files_versions" not in enabled
    assert "trashbin:cleanup --all-users" in calls
    assert calls.index("trashbin:cleanup --all-users") < calls.index("app:disable files_trashbin")
    assert "app:enable files_trashbin" not in calls


def test_rerun_with_trash_already_off_changes_nothing(tmp_path):
    res, calls, enabled = run(tmp_path, ["files", "files_pdfviewer", "text", "twofactor_totp", "skpods"])
    assert res.returncode == 0, res.stdout[-3000:]
    assert calls == []
    assert "files_trashbin" not in enabled


def test_enable_apps_override_keeps_trash_and_never_empties_it(tmp_path):
    keep = ["files_pdfviewer", "files_trashbin", "text", "twofactor_totp", "skpods"]
    res, calls, enabled = run(tmp_path, ["files", "files_trashbin"], {"ENABLE_APPS": keep})
    assert res.returncode == 0, res.stdout[-3000:]
    assert "files_trashbin" in enabled
    assert not any(c.startswith("trashbin:cleanup") or c == "app:disable files_trashbin" for c in calls)


def test_failed_cleanup_stops_before_the_trash_app_is_disabled(tmp_path):
    res, calls, enabled = run(tmp_path, ["files", "files_trashbin"], cleanup_fails=True)
    assert res.returncode != 0
    assert "files_trashbin" in enabled
    assert "app:disable files_trashbin" not in calls


def test_default_lists():
    setf = tasks()[0]["vars"]
    assert {"files_trashbin", "files_versions"} <= set(setf["skp_default_disable_apps"])
    assert not {"files_trashbin", "files_versions"} & set(setf["skp_default_enable_apps"])
