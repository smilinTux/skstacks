"""skhome's backup script, run for real under /bin/bash, against a faked
`npx tsx scripts/backup.ts` / `scripts/backup-verify.ts` (HomeCenter's own
backup engine). The script never copies the raw SQLite file: it shells out
to the app's own backup CLI, then its own verify CLI, and only logs success
when both agree. A failed backup, a success with no parseable output path,
or a failed verification must all be loud failures that leave no file this
script claims is good.
"""
import os
import pathlib
import stat
import subprocess

import pytest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "ansible/optional/skhome/src/skhome/skhome-backup"
FAKEBIN = HERE / "render" / "fakebin"


def run(tmp, *args, extra_env=None):
    env = dict(os.environ)
    env["PATH"] = f"{FAKEBIN}:{env['PATH']}"
    env["SKHOME_APP_DIR"] = str(tmp)
    env["SKHOME_BACKUP_OUT"] = str(tmp / "backup")
    env.update(extra_env or {})
    (tmp / "backup").mkdir(exist_ok=True)
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)


@pytest.fixture(autouse=True)
def fake_npx_is_executable():
    npx = FAKEBIN / "npx"
    npx.chmod(npx.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def backups(tmp):
    return sorted((tmp / "backup").glob("homecenter-*.hcbackup"))


def test_once_runs_backup_then_verify_and_logs_success(tmp_path):
    r = run(tmp_path, "once")
    assert r.returncode == 0, r.stdout + r.stderr
    (archive,) = backups(tmp_path)
    assert archive.read_text() == "fake backup contents\n"
    assert "wrote and verified" in r.stdout
    assert str(archive) in r.stdout


def test_a_failed_backup_writes_no_file_and_is_a_loud_failure(tmp_path):
    r = run(tmp_path, "once", extra_env={"FAKE_BACKUP_RC": "1"})
    assert r.returncode == 1
    assert backups(tmp_path) == []
    assert "ERROR: backup failed" in r.stdout


def test_success_with_no_written_line_is_treated_as_a_failure(tmp_path):
    # The fake engine still drops a file on disk (as the real one would),
    # but if this script can't parse which file it was, it must not claim
    # success: a later prune by the app's own retention could remove the
    # very file skbackup was told to trust.
    r = run(tmp_path, "once", extra_env={"FAKE_NO_WRITTEN_LINE": "1"})
    assert r.returncode == 1
    assert "printed no 'Backup written:' line" in r.stdout


def test_a_failed_verification_is_a_loud_failure(tmp_path):
    r = run(tmp_path, "once", extra_env={"FAKE_VERIFY_RC": "1"})
    assert r.returncode == 1
    assert "failed verification" in r.stdout
    assert "fake corruption" in r.stdout
    # The (unverified) file is still on disk -- this script does not delete
    # it -- but it is never reported as good.
    assert "wrote and verified" not in r.stdout


def test_each_run_gets_its_own_file(tmp_path):
    assert run(tmp_path, "once").returncode == 0
    assert run(tmp_path, "once").returncode == 0
    assert len(backups(tmp_path)) == 2


def test_unknown_subcommand_is_a_usage_error(tmp_path):
    r = run(tmp_path, "banana")
    assert r.returncode == 2
    assert "usage: skhome-backup" in r.stderr
