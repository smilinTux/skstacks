"""The files_antivirus app's own scan-size settings (av_max_file_size,
av_stream_max_length) must track clamd's StreamMaxLength (test_skhub_clamav_tuning
covers the clamd.conf side), but only when files_antivirus is actually installed and
enabled: NAM runs the clamav container without ever installing files_antivirus, so an
unconditional occ config:app:set would write orphaned appconfig rows for an app that is
not there. This task is gated on a new skhub.enable_files_antivirus flag, the same
pattern already used for skhub.enable_collabora / skhub.enable_talk_hpb. It is additional
to (not a replacement for) the existing unconditional "Configure ClamAV antivirus host in
Nextcloud" task, which sets av_host/av_port/av_mode and is left untouched.
"""
import pathlib

import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
PLAYBOOKS = sorted(SKHUB.glob("deploy_skhub-*.yml"))


def _tasks(playbook):
    plays = yaml.safe_load(playbook.read_text())
    return [t for play in plays for t in play.get("tasks", [])]


def _shell(task):
    cmd = task.get("shell") or task.get("command") or ""
    return cmd if isinstance(cmd, str) else str(cmd)


def _find(playbook, name):
    matches = [t for t in _tasks(playbook) if t.get("name") == name]
    assert len(matches) == 1, f"{playbook.name}: expected exactly one task named {name!r}, found {len(matches)}"
    return matches[0]


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_all_three_envs_present(playbook):
    assert playbook.exists()


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_scan_size_task_exists_and_is_gated(playbook):
    task = _find(playbook, "Tune ClamAV scan size limits in Nextcloud (files_antivirus)")
    when = str(task.get("when", ""))
    assert "enable_files_antivirus" in when


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_scan_size_task_sets_both_values(playbook):
    shell = _shell(_find(playbook, "Tune ClamAV scan size limits in Nextcloud (files_antivirus)"))
    assert "av_max_file_size" in shell
    assert "av_stream_max_length" in shell


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_scan_size_task_derives_bytes_from_the_same_mb_knob_as_clamd_conf(playbook):
    """Keeps clamd's StreamMaxLength and the app's av_stream_max_length aligned
    from one source instead of two numbers that can drift apart."""
    shell = _shell(_find(playbook, "Tune ClamAV scan size limits in Nextcloud (files_antivirus)"))
    assert "CLAMD_STREAM_MAX_LENGTH_MB" in shell
    assert "1048576" in shell


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_scan_size_task_is_best_effort(playbook):
    """Must never block the deploy: same convention as the other optional
    post-deploy app-tuning tasks in this playbook."""
    task = _find(playbook, "Tune ClamAV scan size limits in Nextcloud (files_antivirus)")
    assert task.get("ignore_errors") in (True, "yes", "true")


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_existing_clamav_host_task_is_unchanged(playbook):
    """The legacy unconditional task (av_host/av_port/av_mode) must keep running
    for every instance, not just ones with enable_files_antivirus set."""
    task = _find(playbook, "Configure ClamAV antivirus host in Nextcloud")
    when = str(task.get("when", ""))
    assert "enable_files_antivirus" not in when
