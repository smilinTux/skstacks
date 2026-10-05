"""Functional tests for the skbackup engine (src/skbackup): the storage-host
scripts lifted from the production setup (sync, restic, check, predeploy,
lib.sh) behind the white-label dispatcher. They run for real against a
scratch tree; only zfs, zpool, mount, umount and mountpoint are test doubles
(fakezfs.py). restic tests use a local repository and skip where restic is
not installed."""
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from skbackup_support import LIB, FakeHost, needs_restic

HOUR = 3600
DAY = 86400


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root: Path, skip: tuple = ()) -> dict[str, str]:
    return {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob("*"))
            if p.is_file() and not str(p.relative_to(root)).startswith(skip)}


def lib(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'. "{LIB}"; {script}'], capture_output=True, text=True)


@pytest.fixture
def host(tmp_path):
    return FakeHost(tmp_path).seed()


# --- lib.sh helpers (ported from the production setup's own tests) ---------

def test_parse_app_line():
    out = lib("skb_parse_app_line 'skgit skgit-prod 14 4 3 --exclude=/packages/ dump=database-dump # c'").stdout
    assert out == "skgit\tskgit-prod\t14\t4\t3\t--exclude=/packages/ dump=database-dump\n"
    assert lib("skb_parse_app_line '# only a comment'").returncode == 1
    assert lib("skb_parse_app_line 'a b 1 2'").returncode == 1
    assert lib("skb_parse_app_line 'bad/name x 1 1 1'").returncode == 2
    assert lib("skb_parse_app_line 'n /abs 1 1 1'").returncode == 2
    assert lib("skb_parse_app_line 'n a/../b 1 1 1'").returncode == 2


def test_prune_list():
    assert lib("printf 'a\\nb\\nc\\nd\\n' | skb_prune_list 2").stdout == "a\nb\n"
    assert lib("printf 'a\\nb\\n' | skb_prune_list 0").stdout == "a\n"
    assert lib("printf 'a\\n' | skb_prune_list 3").stdout == ""


def test_restic_excludes():
    out = lib("skb_restic_excludes /m/app --exclude=/packages/ --exclude='index-v2*' newest=d:1 dump=d").stdout
    assert out == "--exclude=/m/app/packages\n--exclude=index-v2*\n"


def test_newest_excludes(tmp_path):
    (tmp_path / "dump").mkdir()
    for i in range(1, 5):
        f = tmp_path / "dump" / f"f{i}"
        f.write_text("x")
        os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))
    out = lib(f'skb_newest_excludes "{tmp_path}" newest=dump:2 newest=nodir:1 | sort').stdout
    assert out == "--exclude=/dump/f1\n--exclude=/dump/f2\n"


# --- CLI, banner, help -----------------------------------------------------

def test_help_uses_brand_and_lists_commands(host):
    out = host.run("help", check=True).stdout
    assert out.splitlines()[0] == "SKBackup"
    assert "Snapshots, local copies and offsite backups" in out
    assert "usage: skbackup <command>" in out
    for cmd in ("sync", "restic init", "restic backup", "restic forget-prune", "restic restore-test",
                "check [--notify]", "predeploy PURPOSE", "status"):
        assert f"  {cmd}" in out, cmd
    assert "SKBackup, part of SKStacks" in out


def test_unknown_command_is_a_usage_error(host):
    proc = host.run("frobnicate")
    assert proc.returncode == 2 and "unknown command" in proc.stderr


def test_missing_config_fails_clearly(host):
    host.conf.unlink()
    proc = host.run("status")
    assert proc.returncode == 2 and "config" in proc.stderr


def test_credits_text_only_when_set(tmp_path):
    assert "Powered by" not in FakeHost(tmp_path / "a").run("help").stdout
    h = FakeHost(tmp_path / "b", BRAND_CREDITS_TEXT="Powered by Something")
    assert "Powered by Something" in h.run("help").stdout


# --- tier 1: pre-deploy helper ---------------------------------------------

def test_predeploy_snapshot(host):
    out = host.run("predeploy", "skgit-upgrade", check=True).stdout
    assert "tank/data@pre-skgit-upgrade-" in out
    assert any(n.startswith("pre-skgit-upgrade-") for n in host.snaps("tank/data"))
    assert "pre-skgit-upgrade-" in host.run("predeploy", "--list", check=True).stdout


def test_predeploy_prunes_old_ones_but_keeps_the_newest_two(host):
    now = int(time.time())
    for i, age in enumerate((60, 50, 40)):
        host.zfs("snapshot", f"tank/data@pre-old{i}-2026", now=now - age * DAY)
    host.run("predeploy", "fresh", check=True)
    pre = [n for n in host.snaps("tank/data") if n.startswith("pre-")]
    assert pre[0] == "pre-old2-2026" and pre[1].startswith("pre-fresh-") and len(pre) == 2


def test_predeploy_rejects_unsafe_purpose(host):
    assert host.run("predeploy", "../../etc").returncode == 2
    assert host.run("predeploy", "Bad Name").returncode == 2


# --- tiers 2 + 3: sync -----------------------------------------------------

def test_sync_copies_each_app_from_a_frozen_snapshot(host):
    # Exercise the daily copy on a non-weekly, non-monthly calendar day.
    clock = host.bin / "date"
    clock.write_text(
        '#!/bin/sh\ncase "$1" in\n'
        '  +%u) echo 5;;\n  +%d) echo 15;;\n'
        '  *) exec /usr/bin/date "$@";;\nesac\n'
    )
    clock.chmod(0o755)
    out = host.run("sync", check=True).stdout
    copy1 = host.copies / "app1"
    assert (copy1 / "a.txt").read_text() == "alpha\n"
    assert sha(copy1 / "sub/b.bin") == sha(host.data / "app1/sub/b.bin")
    assert not (copy1 / "cache").exists(), "per-app exclude ignored"
    assert sorted(p.name for p in (copy1 / "dump").iterdir()) == ["db-1.sql", "db-2.sql"], "newest=dump:2 ignored"
    assert (host.copies / "app2/c.txt").exists() and not (host.copies / "app3").exists()
    assert [n.split("-")[0] for n in host.snaps("backup/copies/app1")] == ["daily"]
    assert not [n for n in host.snaps("tank/data") if n.startswith("skbackup-")], "transient snapshot left"
    assert (host.state / "last-ok-app1").exists()
    assert "done: 2 app(s) copied, fail=0" in out


def test_sync_prunes_history_to_the_app_retention(tmp_path):
    host = FakeHost(tmp_path, apps=["app1 app1 2 0 0"]).seed()
    host.zfs("create", "-p", "backup/copies/app1")
    for d in (3, 2, 1):
        host.zfs("snapshot", f"backup/copies/app1@daily-old{d}", now=int(time.time()) - d * DAY)
    host.run("sync", check=True)
    daily = [n for n in host.snaps("backup/copies/app1") if n.startswith("daily-")]
    assert len(daily) == 2 and daily[0] == "daily-old1"


def test_dump_hooks_run_before_the_freeze(tmp_path):
    host = FakeHost(tmp_path, hooks=[
        f"app2-db 60 mkdir -p {tmp_path}/tank/data/app2/dump && echo dumped > {tmp_path}/tank/data/app2/dump/db.sql"]).seed()
    host.run("sync", check=True)
    assert (host.copies / "app2/dump/db.sql").read_text() == "dumped\n"


def test_failed_or_slow_hook_is_recorded_and_the_copy_still_runs(tmp_path):
    host = FakeHost(tmp_path, hooks=["broken 60 false", "slow 1 sleep 30"]).seed()
    host.run("sync", check=True)
    assert (host.copies / "app1/a.txt").exists()
    assert (host.state / "hook-failed-broken").exists() and (host.state / "hook-failed-slow").exists()
    host.sanoid()
    out = host.run("check").stdout
    assert "WARN tier3: dump hook broken failed" in out and "WARN tier3: dump hook slow failed" in out


def test_stale_dump_is_flagged_not_fatal(tmp_path):
    host = FakeHost(tmp_path, apps=["app1 app1 7 4 3 dump=dump", "app2 app2 7 4 3"]).seed()
    for f in (host.data / "app1/dump").iterdir():
        os.utime(f, (time.time() - 40 * HOUR,) * 2)
    host.run("sync", check=True)
    assert (host.state / "dump-stale-app1").exists() and (host.copies / "app2/c.txt").exists()
    host.sanoid()
    assert "WARN tier3: app1 dump older than 26h" in host.run("check").stdout


def test_a_second_sync_is_refused_while_one_holds_the_lock(host):
    with open(host.root / "lock" / "skbackup-sync.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = host.run("sync")
    assert proc.returncode == 1 and "holds the lock" in proc.stderr


# --- tier 6: check ---------------------------------------------------------

def test_check_is_quiet_when_fresh(host):
    host.sanoid()
    host.run("sync", check=True)
    (host.state / "restic-last-ok-kept").write_text(str(int(time.time())))
    (host.state / "restic-last-ok-photos").write_text(str(int(time.time())))
    proc = host.run("check", "--notify")
    assert proc.returncode == 0, proc.stdout
    assert "STALE" not in proc.stdout and host.alerts() == ""
    assert "RESULT: all fresh" in (host.state / "reports/check-latest.txt").read_text()


def test_check_flags_stale_tiers(host):
    host.sanoid(now=int(time.time()) - 3 * HOUR)
    host.run("sync", check=True)
    for snap in host.snaps("backup/copies/app1"):
        host.set_creation(f"backup/copies/app1@{snap}", int(time.time()) - 30 * HOUR)
    (host.state / "restic-last-ok-kept").write_text(str(int(time.time()) - 40 * HOUR))
    out = host.run("check").stdout
    assert "STALE tier1: newest tank/data@autosnap_" in out
    assert "STALE tier2: app1 newest" in out
    assert "STALE tier4: restic set kept last ok 40h ago" in out
    assert "STALE tier4: restic set photos never completed" in out


def test_check_seed_grace_and_missing_copies(host):
    host.sanoid()
    (host.state / "restic-seed-started-kept").write_text(str(int(time.time())))
    out = host.run("check").stdout
    assert "PENDING tier4: kept first upload in progress" in out
    assert "STALE tier2: app1 has no daily snapshot" in out


def test_check_warns_on_pool_health_and_capacity(host):
    host.sanoid()
    host.set_pool("tank", cap="91")
    host.set_pool("backup", health="DEGRADED")
    out = host.run("check").stdout
    assert "WARN pool tank 91% full" in out and "WARN pool backup health DEGRADED" in out


def test_check_notify_sends_brand_prefixed_alerts(tmp_path):
    rec = tmp_path / "rec.sh"
    rec.write_text('#!/bin/sh\nfor a in "$@"; do printf "<%s>\\n" "$a"; done >> "$REC_OUT"\n')
    rec.chmod(rec.stat().st_mode | stat.S_IEXEC)
    host = FakeHost(tmp_path, NOTIFY_MODE="command", NOTIFY_COMMAND=f"{rec} -l {{level}} -k {{key}} {{message}}").seed()
    out = tmp_path / "rec.out"
    host.run("check", "--notify", env={"REC_OUT": str(out)})
    lines = out.read_text().splitlines()
    assert lines[:2] == ["<-l>", "<crit>"] and lines[2] == "<-k>" and lines[3].startswith("<skbackup-tier1-")
    assert lines[4].startswith("<[SKBackup] crit: tier1: no autosnap_") and lines[4].endswith(">")
    assert "SKBackup, part of SKStacks" in host.alerts(), "the local alert log always gets the footer"


def test_check_without_notify_sends_nothing(host):
    host.run("check")
    assert host.alerts() == ""


def test_notifier_failure_is_reported(tmp_path):
    host = FakeHost(tmp_path, NOTIFY_MODE="command", NOTIFY_COMMAND="false {message}").seed()
    proc = host.run("check", "--notify")
    assert proc.returncode == 1 and "not delivered" in proc.stderr


def test_status(host):
    host.sanoid()
    host.run("sync", check=True)
    out = host.run("status", check=True).stdout
    assert out.startswith("SKBackup\n") and "app1" in out and "last copy 0h ago" in out
    assert "autosnap_" in out and "photos" in out


# --- tiers 4 + 5: restic -------------------------------------------------------

@pytest.fixture
def off(host):
    host.run("restic", "init", check=True)
    host.sanoid()
    return host


@needs_restic
def test_init_is_idempotent(off):
    out = off.run("restic", "init", check=True).stdout
    assert "already initialised" in out


@needs_restic
def test_backup_from_the_newest_snapshot_restores_byte_for_byte(off, tmp_path):
    off.run("restic", "backup", check=True)
    assert not off.mnt.is_symlink(), "the snapshot was left mounted"
    dest = tmp_path / "restored"
    env = off.env(extra={"RESTIC_REPOSITORY": str(off.repo), "RESTIC_PASSWORD": "test-restic-password"})
    subprocess.run(["restic", "restore", "latest", "--tag", "kept", "--target", str(dest)], env=env,
                   check=True, capture_output=True)
    got = dest / str(off.mnt).lstrip("/")
    assert tree(got / "app1") == tree(off.data / "app1", skip=("cache/", "dump/db-0"))
    assert tree(got / "app2") == tree(off.data / "app2")
    assert (off.state / "restic-last-ok-kept").exists() and (off.state / "restic-last-ok-photos").exists()
    assert (off.state / "restic-src-kept").read_text().strip() == "tank/data@autosnap_2027-01-15_08:00:00_hourly"


@needs_restic
def test_snapshots_carry_set_and_brand_tags_and_host(off):
    off.run("restic", "backup", check=True)
    out = off.run("restic", "snapshots", check=True).stdout
    assert "kept" in out and "photos" in out and "skbackup" in out and "test-host" in out


@needs_restic
def test_second_backup_uses_the_parent(off):
    off.run("restic", "backup", check=True)
    off.sanoid(name="autosnap_2027-01-15_09:00:00_hourly")
    proc = off.run("restic", "backup", "kept", check=True)
    assert "using parent snapshot" in proc.stdout + proc.stderr


@needs_restic
def test_restore_test_passes_reports_and_notifies(off):
    off.run("restic", "backup", check=True)
    proc = off.run("restic", "restore-test", check=True)
    assert "RESULT: PASS" in proc.stdout
    report = sorted((off.state / "reports").glob("restore-test-*.txt"))[-1].read_text()
    assert report.startswith("SKBackup: restore test") and "SKBackup, part of SKStacks" in report
    assert "info skbackup-restore-test [SKBackup] info: restore test passed" in off.alerts()
    assert (off.state / "restic-restoretest-ok").exists()


@needs_restic
def test_restore_test_catches_a_mismatch(off):
    off.run("restic", "backup", check=True)
    ref = off.data / ".zfs/snapshot/autosnap_2027-01-15_08:00:00_hourly/app1/a.txt"
    ref.write_text("tampered\n")
    proc = off.run("restic", "restore-test")
    assert proc.returncode == 1 and "RESULT: FAIL" in proc.stdout and "MISMATCH app1/a.txt" in proc.stdout
    assert "crit skbackup-restore-test [SKBackup] crit: restore test FAILED" in off.alerts()


@needs_restic
def test_check_asks_the_repository_when_configured(tmp_path):
    host = FakeHost(tmp_path, CHECK_RESTIC_REPO=1).seed()
    host.run("restic", "init", check=True)
    host.sanoid()
    host.run("restic", "backup", check=True)
    assert "OK tier4: repository kept 0h" in host.run("check").stdout
    stale = FakeHost(tmp_path, CHECK_RESTIC_REPO=1, MAX_AGE_RESTIC_H=0)
    assert "STALE tier4: repository newest snapshot of set kept is 0h old" in stale.run("check").stdout
    host.env_file.write_text(f"RESTIC_REPOSITORY={tmp_path}/nope\nRESTIC_PASSWORD=x\n")
    out = host.run("check")
    assert out.returncode == 1 and "STALE tier4: repository unreachable for set kept" in out.stdout


@needs_restic
def test_forget_prune_keeps_the_retention(tmp_path):
    host = FakeHost(tmp_path, RESTIC_KEEP_DAILY=1, RESTIC_KEEP_WEEKLY=0, RESTIC_KEEP_MONTHLY=0,
                    sets=["kept apps"]).seed()
    host.run("restic", "init", check=True)
    for h in range(3):
        (host.data / "app2/c.txt").write_text(f"v{h}\n")
        host.sanoid(name=f"autosnap_2027-01-1{h}_08:00:00_daily")
        host.run("restic", "backup", check=True)
    host.run("restic", "forget-prune", check=True)
    env = host.env(extra={"RESTIC_REPOSITORY": str(host.repo), "RESTIC_PASSWORD": "test-restic-password"})
    snaps = json.loads(subprocess.run(["restic", "snapshots", "--json"], env=env, capture_output=True,
                                      text=True, check=True).stdout)
    assert len(snaps) == 1
