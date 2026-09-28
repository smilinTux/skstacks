"""Functional tests for the skbackup engine (src/skbackup/backup) on a scratch
tree with the `dir` snapshot backend: no ZFS, no root, no network. restic
tests use a local repository and skip where restic is not installed.

Time is injected with BK_NOW (epoch seconds), so retention and staleness
are tested without sleeping."""
import fcntl
import hashlib
import os
import stat
from pathlib import Path

import pytest

from skbackup_support import Engine, needs_restic

T0 = 1_800_000_000          # a fixed "now" (2027-01-15, a Friday)
DAY = 86_400


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def eng(tmp_path):
    return Engine(tmp_path).seed()


# --- CLI, banner, help -----------------------------------------------------

def test_help_uses_brand_and_lists_commands(eng):
    out = eng.run("help", check=True).stdout
    assert out.splitlines()[0].startswith("SKBackup ")
    assert "Snapshots, local copies and offsite backups" in out
    assert "usage: skbackup " in out
    for cmd in ("run", "snapshot", "predeploy", "copy", "offsite", "prune", "restore-test", "restore", "check", "status"):
        assert f"  {cmd}" in out, cmd


def test_unknown_command_is_a_usage_error(eng):
    proc = eng.run("frobnicate")
    assert proc.returncode == 2 and "unknown command" in proc.stderr


def test_missing_config_fails_clearly(tmp_path):
    eng = Engine(tmp_path)
    eng.conf.unlink()
    proc = eng.run("status")
    assert proc.returncode == 2 and "config" in proc.stderr


def test_credits_text_only_when_set(tmp_path):
    assert "Powered by" not in Engine(tmp_path / "a").run("help").stdout
    eng = Engine(tmp_path / "b", BRAND_CREDITS_TEXT="Powered by Something")
    assert "Powered by Something" in eng.run("help").stdout


# --- tier 2: copy from a frozen snapshot ------------------------------------

def test_run_copies_each_app_from_a_frozen_snapshot_and_drops_the_transient_one(eng):
    eng.run("run", now=T0, check=True)
    copy1 = eng.copies / "app1"
    assert (copy1 / "a.txt").read_text() == "alpha\n"
    assert sha(copy1 / "sub/b.bin") == sha(eng.data / "app1/sub/b.bin")
    assert not (copy1 / "cache").exists(), "per-app exclude ignored"
    assert (eng.copies / "app2/c.txt").exists()
    assert not (eng.copies / "app3").exists(), "app not selected was copied"
    # history snapshot per app, transient data snapshot removed
    assert [s for s in eng.snaps(copy1)] == ["skbackup-daily-20270115T080000Z"]
    assert not [s for s in eng.snaps(eng.data) if "-run-" in s]
    assert (eng.state / "last-ok-copy-app1").read_text().strip() == str(T0)


def test_copy_deletes_what_the_source_deleted_but_history_keeps_it(eng):
    eng.run("run", now=T0, check=True)
    (eng.data / "app2/c.txt").unlink()
    eng.run("run", now=T0 + DAY, check=True)
    assert not (eng.copies / "app2/c.txt").exists()
    snaps = eng.snaps(eng.copies / "app2")
    assert len(snaps) == 2
    first = eng.root / "snaps" / str(eng.copies / "app2").strip("/").replace("/", "_") / snaps[0]
    assert (first / "c.txt").read_text() == "gamma\n"


def test_copy_history_is_pruned_per_app_retention(tmp_path):
    eng = Engine(tmp_path, COPY_APPS=["app1|2|0|0", "app2|4|0|0"]).seed()
    for d in range(5):
        eng.run("run", now=T0 + d * DAY, check=True)
    assert len(eng.snaps(eng.copies / "app1")) == 2
    assert len(eng.snaps(eng.copies / "app2")) == 4
    assert eng.snaps(eng.copies / "app1")[-1].endswith("20270119T080000Z")


def test_weekly_and_monthly_history_on_their_days(tmp_path):
    eng = Engine(tmp_path, COPY_APPS=["app1|7|4|3"]).seed()
    sunday = T0 + 2 * DAY          # 2027-01-17
    first_of_month = T0 + 17 * DAY  # 2027-02-01
    eng.run("run", now=sunday, check=True)
    eng.run("run", now=first_of_month, check=True)
    names = eng.snaps(eng.copies / "app1")
    assert any("-weekly-20270117" in n for n in names)
    assert any("-monthly-20270201" in n for n in names)


# --- tier 3: dump hooks and freshness gates ---------------------------------

def test_dump_hooks_run_before_the_snapshot(tmp_path):
    eng = Engine(tmp_path, DUMPS_ENABLED=True).seed()
    hook = f"sh -c 'mkdir -p {eng.data}/app1/dump && echo dumped > {eng.data}/app1/dump/db.sql'"
    eng = Engine(tmp_path, DUMPS_ENABLED=True, DUMP_HOOKS=[f"app1-db|60|{hook}"],
                 DUMP_CHECKS=["app1|dump/*.sql|26"])
    eng.run("run", check=True)   # real time: the hook's dump gets a real mtime
    assert (eng.copies / "app1/dump/db.sql").read_text() == "dumped\n"


def test_failed_hook_aborts_the_run_and_alerts(tmp_path):
    eng = Engine(tmp_path, DUMPS_ENABLED=True, DUMP_HOOKS=["broken|60|false"]).seed()
    proc = eng.run("run", now=T0)
    assert proc.returncode == 1
    assert not (eng.copies / "app1").exists()
    assert "crit" in eng.alerts() and "broken" in eng.alerts()


def test_failed_hook_does_not_abort_when_dumps_not_required(tmp_path):
    eng = Engine(tmp_path, DUMPS_ENABLED=True, DUMPS_REQUIRED=False, DUMP_HOOKS=["broken|60|false"]).seed()
    proc = eng.run("run", now=T0)
    assert proc.returncode == 1          # still reported as a failed run
    assert (eng.copies / "app1/a.txt").exists()


def test_hook_timeout_is_enforced(tmp_path):
    eng = Engine(tmp_path, DUMPS_ENABLED=True, DUMP_HOOKS=["slow|1|sleep 30"]).seed()
    proc = eng.run("run", now=T0)
    assert proc.returncode == 1 and "slow" in eng.alerts()


def test_stale_or_empty_dump_refuses_the_snapshot(tmp_path):
    eng = Engine(tmp_path, DUMPS_ENABLED=True, DUMP_CHECKS=["app1|dump/*.sql|26"]).seed()
    d = eng.data / "app1/dump"
    d.mkdir()
    (d / "db.sql").write_text("x\n")
    os.utime(d / "db.sql", (T0 - 30 * 3600, T0 - 30 * 3600))
    proc = eng.run("run", now=T0)
    assert proc.returncode == 1 and "stale" in eng.alerts().lower()
    assert not (eng.copies / "app1").exists()
    (d / "db.sql").write_text("")
    os.utime(d / "db.sql", (T0, T0))
    eng.run("run", now=T0)
    assert "empty" in eng.alerts().lower()
    (d / "db.sql").write_text("x\n")
    os.utime(d / "db.sql", (T0 - 3600, T0 - 3600))
    eng.run("run", now=T0, check=True)


# --- tier 1: builtin snapshots and the pre-deploy helper ---------------------

def test_builtin_snapshot_keeps_the_newest_n(eng):
    for h in range(5):
        eng.run("snapshot", now=T0 + h * 3600, check=True)
    names = eng.snaps(eng.data)
    auto = [n for n in names if "-auto-" in n]
    assert len(auto) == 3 and auto[-1] == "skbackup-auto-20270115T120000Z"
    snap = eng.root / "snaps" / str(eng.data).strip("/").replace("/", "_") / auto[-1]
    assert tree(snap / "app1") == tree(eng.data / "app1")


def test_dir_snapshot_is_frozen_against_later_writes(eng):
    eng.run("snapshot", now=T0, check=True)
    (eng.data / "app1/a.txt").write_text("changed in place\n")
    snap = eng.root / "snaps" / str(eng.data).strip("/").replace("/", "_") / "skbackup-auto-20270115T080000Z"
    assert (snap / "app1/a.txt").read_text() == "alpha\n"


def test_predeploy_snapshot_and_age_prune_keeps_the_newest_two(eng):
    for d in (0, 1, 40, 50):
        eng.run("predeploy", f"deploy{d}", now=T0 - (60 - d) * DAY, check=True)
    out = eng.run("predeploy", "upgrade-x", now=T0, check=True).stdout
    assert "pre-upgrade-x-20270115T080000Z" in out
    pre = [n for n in eng.snaps(eng.data) if n.startswith("pre-")]
    # 60/59 days old pruned (>30d), 20/10 days old kept, plus the new one
    assert pre == sorted(["pre-deploy40-20261226T080000Z", "pre-deploy50-20270105T080000Z",
                          "pre-upgrade-x-20270115T080000Z"])


def test_predeploy_never_prunes_below_the_minimum(tmp_path):
    eng = Engine(tmp_path, PREDEPLOY_KEEP_DAYS=1, PREDEPLOY_KEEP_MIN=2).seed()
    for d in (0, 1, 2):
        eng.run("predeploy", f"p{d}", now=T0 - (90 - d) * DAY, check=True)
    assert len([n for n in eng.snaps(eng.data) if n.startswith("pre-")]) == 2


def test_predeploy_rejects_unsafe_purpose(eng):
    proc = eng.run("predeploy", "../../etc", now=T0)
    assert proc.returncode == 2


# --- locking ----------------------------------------------------------------

def test_a_second_run_is_refused_while_one_holds_the_lock(eng):
    with open(eng.root / "lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = eng.run("run", now=T0)
    assert proc.returncode == 75 and "another run" in proc.stderr


# --- tier 6: staleness checks -----------------------------------------------

def test_check_is_quiet_when_fresh(eng):
    eng.run("snapshot", now=T0, check=True)
    eng.run("run", now=T0, check=True)
    proc = eng.run("check", now=T0 + 3600)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "crit" not in eng.alerts()
    assert (eng.state / "reports/check-latest.txt").exists()


def test_check_alerts_on_stale_snapshot_and_copy(eng):
    eng.run("snapshot", now=T0, check=True)
    eng.run("run", now=T0, check=True)
    proc = eng.run("check", now=T0 + 2 * DAY)
    assert proc.returncode == 2
    alerts = eng.alerts()
    assert "[SKBackup] crit: snapshot" in alerts
    assert "copy app1" in alerts and "copy app2" in alerts
    assert "SKBackup, part of SKStacks" in alerts   # footer in the body


def test_check_alerts_on_missing_copy(eng):
    eng.run("snapshot", now=T0, check=True)
    proc = eng.run("check", now=T0)
    assert proc.returncode == 2 and "copy app1" in eng.alerts()


def test_check_alerts_on_stale_dump(tmp_path):
    eng = Engine(tmp_path, DUMPS_ENABLED=True, DUMP_CHECKS=["app1|dump/*.sql|26"], COPY_ENABLED=False).seed()
    eng.run("snapshot", now=T0, check=True)
    (eng.data / "app1/dump").mkdir()
    (eng.data / "app1/dump/x.sql").write_text("x")
    os.utime(eng.data / "app1/dump/x.sql", (T0 - 40 * 3600,) * 2)
    assert eng.run("check", now=T0).returncode == 2
    assert "dump app1" in eng.alerts()


def test_check_warns_on_pool_capacity(tmp_path):
    eng = Engine(tmp_path, POOL_CAP_WARN_PCT=0, COPY_ENABLED=False).seed()
    eng.run("snapshot", now=T0, check=True)
    proc = eng.run("check", now=T0)
    assert proc.returncode == 1 and "warn: capacity" in eng.alerts()


def test_command_notifier_gets_one_argument_per_placeholder_word(tmp_path):
    rec = tmp_path / "rec.sh"
    rec.write_text('#!/bin/sh\nfor a in "$@"; do printf "<%s>\\n" "$a"; done >> "$REC_OUT"\n')
    rec.chmod(rec.stat().st_mode | stat.S_IEXEC)
    eng = Engine(tmp_path, NOTIFY_MODE="command", NOTIFY_COMMAND=f"{rec} -l {{level}} -k {{key}} {{message}}").seed()
    out = tmp_path / "rec.out"
    eng.run("snapshot", now=T0, check=True)
    eng.run("check", now=T0 + 2 * DAY, env={"REC_OUT": str(out)})
    lines = out.read_text().splitlines()
    assert lines[:4] == ["<-l>", "<crit>", "<-k>", "<skbackup-snapshot>"]
    assert lines[4].startswith("<[SKBackup] crit: snapshot") and lines[4].endswith(">")
    assert eng.alerts(), "the local alert log is always written too"


def test_notifier_failure_does_not_hide_the_finding(tmp_path):
    eng = Engine(tmp_path, NOTIFY_MODE="command", NOTIFY_COMMAND="false {message}").seed()
    eng.run("snapshot", now=T0, check=True)
    proc = eng.run("check", now=T0 + 2 * DAY)
    assert proc.returncode == 2 and "notifier failed" in proc.stderr


# --- restore ----------------------------------------------------------------

def test_restore_from_copy_history(eng, tmp_path):
    eng.run("run", now=T0, check=True)
    dest = tmp_path / "restored"
    eng.run("restore", "copy", "app1", "skbackup-daily-20270115T080000Z", str(dest), check=True)
    assert tree(dest) == {k: v for k, v in tree(eng.data / "app1").items() if not k.startswith("cache/")}


def test_restore_from_a_data_snapshot(eng, tmp_path):
    eng.run("snapshot", now=T0, check=True)
    dest = tmp_path / "r"
    eng.run("restore", "snapshot", "app3", "skbackup-auto-20270115T080000Z", str(dest), check=True)
    assert tree(dest) == tree(eng.data / "app3")


def test_restore_refuses_the_live_data_path_and_non_empty_targets(eng, tmp_path):
    eng.run("snapshot", now=T0, check=True)
    proc = eng.run("restore", "snapshot", "app3", "skbackup-auto-20270115T080000Z", str(eng.data / "app3"))
    assert proc.returncode == 2 and "live" in proc.stderr
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    proc = eng.run("restore", "snapshot", "app3", "skbackup-auto-20270115T080000Z", str(busy))
    assert proc.returncode == 2 and "not empty" in proc.stderr


# --- tier 4 + 5: restic ------------------------------------------------------

@pytest.fixture
def off(tmp_path):
    eng = Engine(tmp_path, OFFSITE_ENABLED=True, RESTORE_TEST_ENABLED=True,
                 OFFSITE_SETS=["keep|copy|app1,app2|nightly|", "photos|data|app3||"]).seed()
    eng.run("init-offsite", check=True)
    return eng


@needs_restic
def test_init_offsite_is_idempotent(off):
    assert off.run("init-offsite").returncode == 0


@needs_restic
def test_offsite_backup_restore_roundtrip_matches_sha256(off, tmp_path):
    off.run("run", now=T0, check=True)
    for set_name, app, src in (("keep", "app1", off.copies / "app1"), ("photos", "app3", off.data / "app3")):
        dest = tmp_path / f"r-{app}"
        off.run("restore", "offsite", set_name, app, str(dest), check=True)
        assert tree(dest) == tree(src), app
    assert (off.state / "last-ok-offsite-keep-app1").exists()


@needs_restic
def test_offsite_snapshots_carry_brand_set_app_and_source_tags(off):
    off.run("run", now=T0, check=True)
    out = off.run("snapshots", check=True).stdout
    assert "skbackup" in out and "set:keep" in out and "app:app1" in out and "nightly" in out
    assert "src:skbackup-daily-20270115T080000Z" in out
    assert "test-host" in out


@needs_restic
def test_second_offsite_run_uses_a_parent(off):
    off.run("run", now=T0, check=True)
    proc = off.run("run", now=T0 + DAY, check=True)
    assert "using parent snapshot" in (proc.stdout + proc.stderr)


@needs_restic
def test_restore_test_passes_and_reports(off):
    off.run("run", now=T0, check=True)
    proc = off.run("restore-test", now=T0, check=True)
    reports = sorted((off.state / "reports").glob("restore-test-*.txt"))
    assert reports
    text = reports[-1].read_text()
    assert "RESULT: PASS" in text and "SKBackup" in text and "SKBackup, part of SKStacks" in text
    assert "restore-test" in off.alerts() and "info" in off.alerts()
    assert (off.state / "last-ok-restore-test").exists()


@needs_restic
def test_restore_test_catches_a_mismatch(off):
    off.run("run", now=T0, check=True)
    snapdir = off.root / "snaps" / str(off.copies / "app1").strip("/").replace("/", "_") / "skbackup-daily-20270115T080000Z"
    target = snapdir / "a.txt"
    target.chmod(0o644)
    target.write_text("tampered\n")
    proc = off.run("restore-test", now=T0)
    assert proc.returncode == 1
    assert "RESULT: FAIL" in sorted((off.state / "reports").glob("restore-test-*.txt"))[-1].read_text()
    assert "crit" in off.alerts()


@needs_restic
def test_check_alerts_on_a_stale_offsite_repo(off):
    off.run("snapshot", now=T0, check=True)
    off.run("run", now=T0, check=True)
    off.run("restore-test", now=T0, check=True)
    assert off.run("check", now=T0 + 3600).returncode == 0, off.alerts()
    proc = off.run("check", now=T0 + 3 * DAY)
    assert proc.returncode == 2
    assert "offsite keep/app1" in off.alerts()


@needs_restic
def test_check_alerts_when_the_repo_is_unreachable(off):
    off.run("snapshot", now=T0, check=True)
    off.run("run", now=T0, check=True)
    off.env_file.write_text(f"RESTIC_REPOSITORY={off.root}/nope\nRESTIC_PASSWORD_FILE={off.pass_file}\n")
    assert off.run("check", now=T0 + 3600).returncode == 2
    assert "offsite" in off.alerts()


@needs_restic
def test_prune_applies_forget_per_set_and_app(tmp_path):
    eng = Engine(tmp_path, OFFSITE_ENABLED=True, OFFSITE_FORGET_ARGS=["--keep-last", "1"],
                 OFFSITE_SETS=["keep|copy|app1||"]).seed()
    eng.run("init-offsite", check=True)
    for d in range(3):
        (eng.data / "app1/a.txt").write_text(f"v{d}\n")
        eng.run("run", now=T0 + d * DAY, check=True)
    eng.run("prune", now=T0 + 3 * DAY, check=True)
    out = eng.run("snapshots", check=True).stdout
    assert out.count("app:app1") == 1
