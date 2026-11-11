"""enable_files_antivirus installs and enables the files_antivirus app and keeps the
first-rollout posture (log only, never delete) unless the operator opts into delete."""
import pathlib

import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
PLAYBOOKS = sorted(SKHUB.glob("deploy_skhub-*.yml"))
INSTALL = "Install and enable the files_antivirus app in Nextcloud"
HOST = "Configure ClamAV antivirus host in Nextcloud"
TUNE = "Tune ClamAV scan size limits in Nextcloud (files_antivirus)"


def _tasks(playbook):
    return [t for play in yaml.safe_load(playbook.read_text()) for t in play.get("tasks", [])]


def _one(playbook, name):
    m = [t for t in _tasks(playbook) if t.get("name") == name]
    assert len(m) == 1
    return m[0]


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_install_task_gated_and_best_effort(playbook):
    t = _one(playbook, INSTALL)
    assert "enable_files_antivirus | default(false)" in str(t["when"])
    assert t.get("ignore_errors") in (True, "yes", "true")
    assert "app:install files_antivirus" in t["shell"]
    assert "app:enable files_antivirus" in t["shell"]


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_install_runs_before_av_config(playbook):
    names = [t.get("name") for t in _tasks(playbook)]
    assert names.index(INSTALL) < names.index(HOST) < names.index(TUNE)


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_infected_action_defaults_to_log_only(playbook):
    sh = _one(playbook, TUNE)["shell"]
    assert "av_infected_action" in sh
    assert "FILES_ANTIVIRUS_INFECTED_ACTION | default('only_log')" in sh
    assert "'delete'" not in sh


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_daemon_mode_over_stack_network(playbook):
    sh = _one(playbook, HOST)["shell"]
    assert 'av_mode --value="daemon"' in sh
    assert "_clamav" in sh and "3310" in sh
