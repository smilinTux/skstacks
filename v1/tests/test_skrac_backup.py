"""skrac's backup script, run for real under /bin/sh against temp data/files
dirs. It must never lose a good archive: every run writes its own file
through a verified temp file, a failed run leaves the previous archives
alone and no partial file behind, retention prunes only after a good
archive of that kind was written, and the large meshcentral-files dir is
archived separately (and only when asked for) from the small, critical
meshcentral-data dir.
"""
import os
import pathlib
import subprocess
import tarfile
import time

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skrac/src/skrac/skrac-backup"
DAY = 86400


def run(tmp, *args, data=None, files=None, keep_days=7, include_files=False):
    env = dict(os.environ,
               SKRAC_BACKUP_DATA=str(data if data is not None else tmp / "data"),
               SKRAC_BACKUP_FILES=str(files if files is not None else tmp / "files"),
               SKRAC_BACKUP_OUT=str(tmp / "backup"),
               BACKUP_KEEP_DAYS=str(keep_days),
               SKRAC_BACKUP_INCLUDE_FILES="1" if include_files else "0")
    return subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def tmp(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text('{"settings":{}}')
    (data / "meshcentral.db").write_text('{"users":[{"id":"u1"}]}')
    (data / "meshcentral.db.tmp").write_text("half a write")
    (data / "agentserver-cert-private.key").write_bytes(b"\x00" * 32)
    files = tmp_path / "files"
    (files / "u1").mkdir(parents=True)
    (files / "u1" / "upload.bin").write_bytes(b"x" * 100)
    (tmp_path / "backup").mkdir()
    return tmp_path


def archives(tmp, name="skrac"):
    # "skrac-*" would also match "skrac-files-*"; archive names are always
    # "<name>-<UTC timestamp starting with 2>.tar.gz".
    return sorted((tmp / "backup").glob(f"{name}-2*.tar.gz"))


def test_once_writes_one_verified_data_archive_without_the_files_dir(tmp):
    r = run(tmp, "once")
    assert r.returncode == 0, r.stdout + r.stderr
    (archive,) = archives(tmp)
    assert oct(archive.stat().st_mode & 0o777) == "0o600"
    with tarfile.open(archive) as t:
        names = set(t.getnames())
    assert {"./config.json", "./meshcentral.db", "./agentserver-cert-private.key"} <= names
    assert "./meshcentral.db.tmp" not in names
    assert archives(tmp, "skrac-files") == []  # INCLUDE_FILES off by default
    assert [p.name for p in (tmp / "backup").iterdir() if p.name.startswith(".")] == []


def test_include_files_writes_a_second_separate_archive(tmp):
    r = run(tmp, "once", include_files=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(archives(tmp, "skrac")) == 1
    (files_archive,) = archives(tmp, "skrac-files")
    with tarfile.open(files_archive) as t:
        assert "./u1/upload.bin" in t.getnames()


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
    assert "ERROR" in r.stdout
    assert archives(tmp) == before
    assert list((tmp / "backup").glob(".*")) == []


def test_data_without_config_json_is_not_a_good_archive(tmp):
    (tmp / "data" / "config.json").unlink()
    r = run(tmp, "once")
    assert r.returncode == 1 and archives(tmp) == []


def test_retention_prunes_only_archives_older_than_keep_days_after_a_good_run(tmp):
    old = tmp / "backup" / "skrac-20200101T000000Z.tar.gz"
    recent = tmp / "backup" / "skrac-20200102T000000Z.tar.gz"
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
