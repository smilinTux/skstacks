"""skbackup must fail the deploy closed, not just skip a Jinja default, when
a required secret is present in the vault but set to the empty string:
test_no_default_secrets.py (repo-wide) only catches a literal `| default(
'...')`, which never applies here since these vars have no default at all
-- so an *unset* vault key already fails via Jinja's undefined lookup, and
these `assert` tasks are what catches the remaining case, a vault key that
exists but is `""`. Every env playbook must carry the same checks (parity
with test_env_playbook_parity.py)."""
import pathlib

import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKBACKUP = ANSIBLE / "optional/skbackup"


def _tasks(playbook):
    for play in yaml.safe_load(playbook.read_text()) or []:
        if not isinstance(play, dict):
            continue
        for key in ("pre_tasks", "tasks", "post_tasks"):
            yield from play.get(key) or []


def _assert_thats(playbook):
    thats = []
    for task in _tasks(playbook):
        a = task.get("assert")
        if a:
            thats.append(" ".join(a.get("that") or []))
    return thats


def test_every_env_asserts_settings_encryption_key_and_ui_password_are_non_empty():
    for env in ("dev", "staging", "prod"):
        thats = _assert_thats(SKBACKUP / f"deploy_skbackup-{env}.yml")
        combined = " ".join(thats)
        assert "skbackup.settings_encryption_key" in combined, env
        assert "skbackup.ui_password" in combined, env
        assert "length > 0" in combined, env


def test_every_env_asserts_declared_jobs_have_a_resolvable_passphrase():
    for env in ("dev", "staging", "prod"):
        thats = _assert_thats(SKBACKUP / f"deploy_skbackup-{env}.yml")
        combined = " ".join(thats)
        assert "item.passphrase" in combined, env
        assert "skbackup.passphrase" in combined, env


def test_job_passphrase_assert_is_looped_over_skbackup_jobs():
    for env in ("dev", "staging", "prod"):
        found = False
        for task in _tasks(SKBACKUP / f"deploy_skbackup-{env}.yml"):
            if task.get("assert") and "item.passphrase" in " ".join(task["assert"].get("that") or []):
                assert "skbackup.jobs" in str(task.get("loop", "")), env
                found = True
        assert found, env
