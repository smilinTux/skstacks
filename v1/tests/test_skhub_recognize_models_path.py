"""skhub: the Recognize model check looks where the app really is.

The deploy skips `occ recognize:download-models` (over 1 GB, and it deletes the
models directory first) when models are already present. It used to look only
in custom_apps/recognize/models. Where the image ships the app under apps/,
that check never found the models and every deploy downloaded them again (an
hour and more on a lab cluster). The task now asks `occ app:getpath recognize`.

These tests run the task's real shell against a stub `docker` that answers
`occ app:getpath` and `find` from a fake filesystem.
"""
import os
import pathlib
import subprocess

import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
PLAYBOOKS = sorted(SKHUB.glob("deploy_skhub-*.yml"))
TASK = "Download Recognize ML models (only when missing)"

STUB = """#!/bin/sh
# docker exec <cid> <cmd...>
shift 2
case "$*" in
  "php occ app:getpath recognize") [ -n "$APP_PATH" ] && echo "$APP_PATH"; [ -n "$APP_PATH" ] ;;
  "php occ recognize:download-models") echo "Downloading models"; echo dl >> "$CALLS" ;;
  find\\ *) d="$2"; [ -n "$(ls -A "$FAKE_ROOT$d" 2>/dev/null)" ] && echo "$d/x" ;;
  *) echo "unexpected: $*" >&2; exit 99 ;;
esac
"""


def task_shell(playbook):
    for play in yaml.safe_load(playbook.read_text()):
        for task in play.get("tasks", []):
            if task.get("name") == TASK:
                return task["shell"].replace("{{ nextcloud_container.stdout | trim }}", "cid")
    raise AssertionError(f"{playbook.name}: task {TASK!r} not found")


def run(tmp_path, playbook, app_path, models_at):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "docker"
    stub.write_text(STUB)
    stub.chmod(0o755)
    root = tmp_path / "root"
    if models_at:
        (root / models_at.lstrip("/")).mkdir(parents=True, exist_ok=True)
        (root / models_at.lstrip("/") / "efficientnetv2").mkdir()
    calls = tmp_path / "calls"
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", APP_PATH=app_path,
               FAKE_ROOT=str(root), CALLS=str(calls))
    out = subprocess.run(["sh", "-c", task_shell(playbook)], env=env, capture_output=True,
                         text=True, check=True).stdout
    return out, calls.exists()


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_models_under_apps_are_found(tmp_path, playbook):
    out, downloaded = run(tmp_path, playbook, "/var/www/html/apps/recognize",
                          "/var/www/html/apps/recognize/models")
    assert not downloaded, out
    assert "already present in /var/www/html/apps/recognize/models" in out


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_models_under_custom_apps_are_found(tmp_path, playbook):
    out, downloaded = run(tmp_path, playbook, "/var/www/html/custom_apps/recognize",
                          "/var/www/html/custom_apps/recognize/models")
    assert not downloaded, out


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_missing_models_are_downloaded(tmp_path, playbook):
    _, downloaded = run(tmp_path, playbook, "/var/www/html/apps/recognize", None)
    assert downloaded


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_getpath_failure_falls_back_to_custom_apps(tmp_path, playbook):
    out, downloaded = run(tmp_path, playbook, "", "/var/www/html/custom_apps/recognize/models")
    assert not downloaded, out


def test_all_three_playbooks_are_covered():
    assert {p.name for p in PLAYBOOKS} >= {
        "deploy_skhub-prod.yml", "deploy_skhub-dev.yml", "deploy_skhub-staging.yml"}
