"""A skhub redeploy must not redo heavy work or loosen the Nextcloud data dir.

1. Recognize models. The post-deploy task ran `occ recognize:download-models`
   on every deploy. That command first deletes the whole models directory
   (`DownloadModelsService::download()` removes `custom_apps/recognize/models`
   recursively), then downloads the model archive (over 1 GB) and extracts it
   again. On a shared filesystem that is gigabytes of unthrottled delete and
   write traffic per deploy, it needs the internet, and because the task is
   best-effort (`|| true`, `ignore_errors`) a failed download silently leaves
   Recognize with no models at all. Recognize fetches its models itself when
   it is installed or upgraded, so the deploy only has to cover the case where
   they are missing: download only when the models directory is absent or
   empty. The test runs the task's own shell against a fake `docker`.

2. Data dir mode. Nextcloud requires its data directory to be 0770 ("Your data
   directory is readable by other users"); the directory task created and
   re-moded it 0755 on every deploy, so every redeploy made the list of user
   directories readable to any account on the shared filesystem until
   Nextcloud's own check put 0770 back.
"""
import os
import pathlib
import re
import stat
import subprocess
import textwrap

import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
PLAYBOOKS = sorted(SKHUB.glob("deploy_skhub-*.yml"))
MODELS = "/var/www/html/custom_apps/recognize/models"


def _tasks(playbook):
    plays = yaml.safe_load(playbook.read_text())
    return [t for play in plays for t in play.get("tasks", [])]


def _model_task(playbook):
    found = [t for t in _tasks(playbook) if "recognize:download-models" in str(t.get("shell", ""))]
    assert len(found) == 1, f"{playbook.name}: expected one recognize:download-models task"
    return found[0]


FAKE_DOCKER = textwrap.dedent(
    """\
    #!/bin/sh
    # Fake docker: `exec <cid> find <path> ...` runs find under $FAKE_ROOT,
    # `exec <cid> php occ recognize:download-models` is logged, not run.
    echo "$*" >> "$FAKE_LOG"
    [ "$1" = exec ] || exit 0
    shift
    while [ "$#" -gt 0 ]; do case "$1" in -u|-e|-w) shift 2;; -*) shift;; *) break;; esac; done
    shift  # container id
    case "$1" in
      find) shift; p=$1; shift; exec find "$FAKE_ROOT$p" "$@";;
      php) case "$*" in *recognize:download-models*) echo DOWNLOAD >> "$FAKE_LOG"; echo "Downloading models";; esac;;
    esac
    exit 0
    """
)


def _run(task, tmp_path, models_state):
    root = tmp_path / "root"
    models = root / MODELS.lstrip("/")
    if models_state in ("empty", "present"):
        models.mkdir(parents=True)
    if models_state == "present":
        (models / "efficientnet_lite4").mkdir()
        (models / "efficientnet_lite4" / "model.json").write_text("{}")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "docker.log"
    log.write_text("")
    shell = re.sub(r"\{\{\s*nextcloud_container\.stdout\s*\|\s*trim\s*\}\}", "cid123", task["shell"])
    assert "{{" not in shell, "unexpected template left in the task shell"
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_ROOT=str(root), FAKE_LOG=str(log))
    res = subprocess.run(["/bin/sh", "-c", shell], env=env, capture_output=True, text=True)
    return res, log.read_text(), models


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_models_present_are_not_redownloaded(playbook, tmp_path):
    res, log, models = _run(_model_task(playbook), tmp_path, "present")
    assert res.returncode == 0, res.stderr
    assert "DOWNLOAD" not in log, f"{playbook.name}: re-downloads models that are already there"
    assert (models / "efficientnet_lite4" / "model.json").exists()


@pytest.mark.parametrize("state", ["missing", "empty"])
@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_models_missing_or_empty_are_downloaded(playbook, state, tmp_path):
    res, log, _ = _run(_model_task(playbook), tmp_path, state)
    assert res.returncode == 0, res.stderr
    assert log.count("DOWNLOAD") == 1, f"{playbook.name}: models {state} but not downloaded"


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_model_check_looks_in_the_container_models_dir(playbook):
    shell = _model_task(playbook)["shell"]
    assert MODELS in shell
    assert "maxdepth 1" in shell, "the presence check must not walk the models tree"


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_nextcloud_data_dir_is_0770(playbook):
    dirs = [t for t in _tasks(playbook) if t.get("name", "").startswith("Create the {{ app }} directories")]
    assert len(dirs) == 1
    items = {i["path"]: i for i in dirs[0]["loop"]}
    data = items["/var/data/{{ app }}-{{ env }}/data"]
    assert (data["owner"], data["group"], str(data["mode"])) == ("www-data", "www-data", "0770")
