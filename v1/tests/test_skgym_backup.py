"""skgym's backup script, run for real under /bin/sh against a temp data dir.

The script is what the `backup` sidecar runs (busybox sh/tar/gzip/find in
the upstream api image; GNU tools here, the script uses only the options
both have). It must never lose a good archive: every run writes its own
file through a verified temp file, a failed run leaves the previous
archives alone and no partial file behind, and retention prunes only after
a good archive was written.
"""
import os
import pathlib
import subprocess
import tarfile
import time

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skgym/src/skgym/skgym-backup"
DAY = 86400


def run(tmp, *args, data=None, keep_days=7):
    env = dict(os.environ,
               SKGYM_BACKUP_DATA=str(data if data is not None else tmp / "data"),
               SKGYM_BACKUP_OUT=str(tmp / "backup"),
               BACKUP_KEEP_DAYS=str(keep_days))
    return subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def tmp(tmp_path):
    data = tmp_path / "data"
    (data / "uploads" / "u1").mkdir(parents=True)
    (data / "secret").write_text("s" * 64 + "\n")
    (data / "db.json").write_text('{"users":[{"id":"u1"}]}')
    (data / "db.json.tmp").write_text("half a write")
    (data / "uploads" / "u1" / "photo.jpg").write_bytes(b"\xff\xd8" + b"x" * 100)
    (tmp_path / "backup").mkdir()
    return tmp_path


def archives(tmp):
    return sorted((tmp / "backup").glob("skgym-*.tar.gz"))


def test_once_writes_one_verified_archive_without_temp_files(tmp):
    r = run(tmp, "once")
    assert r.returncode == 0, r.stdout + r.stderr
    (archive,) = archives(tmp)
    assert oct(archive.stat().st_mode & 0o777) == "0o600"
    with tarfile.open(archive) as t:
        names = set(t.getnames())
    assert {"./secret", "./db.json", "./uploads/u1/photo.jpg"} <= names
    assert "./db.json.tmp" not in names
    assert [p.name for p in (tmp / "backup").iterdir() if p.name.startswith(".")] == []


def test_each_run_gets_its_own_archive(tmp):
    assert run(tmp, "once").returncode == 0
    time.sleep(1.1)
    assert run(tmp, "once").returncode == 0
    assert len(archives(tmp)) == 2


def test_failed_run_keeps_the_previous_archives_and_leaves_no_partial(tmp):
    assert run(tmp, "once").returncode == 0
    before = archives(tmp)
    old = before[0]
    os.utime(old, (time.time() - 30 * DAY,) * 2)   # old enough to prune, if pruning ran
    r = run(tmp, "once", data=tmp / "missing")
    assert r.returncode == 1
    assert "backup failed" in r.stdout
    assert archives(tmp) == before
    assert list((tmp / "backup").glob(".*")) == []


def test_data_without_the_secret_is_not_a_good_archive(tmp):
    (tmp / "data" / "secret").unlink()
    r = run(tmp, "once")
    assert r.returncode == 1 and archives(tmp) == []


def test_retention_prunes_only_archives_older_than_keep_days_after_a_good_run(tmp):
    old = tmp / "backup" / "skgym-20200101T000000Z.tar.gz"
    recent = tmp / "backup" / "skgym-20200102T000000Z.tar.gz"
    other = tmp / "backup" / "not-ours.tar.gz"
    for p, age in ((old, 10), (recent, 2), (other, 30)):
        p.write_bytes(b"x")
        os.utime(p, (time.time() - age * DAY,) * 2)
    assert run(tmp, "once", keep_days=7).returncode == 0
    names = {p.name for p in (tmp / "backup").iterdir()}
    assert old.name not in names and recent.name in names and other.name in names
    assert len(archives(tmp)) == 2


def test_unknown_mode_is_a_usage_error(tmp):
    r = run(tmp, "bogus")
    assert r.returncode == 2 and "usage" in r.stderr
