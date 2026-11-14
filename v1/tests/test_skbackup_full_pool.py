"""Full-pool safety of the skbackup copy tier (sync + check).

Regression tests for a backup pool that filled and could not recover:
every copy failed on ENOSPC, the retention prune only ran after a
successful copy (so nothing ever expired), one app's rsync hung until the
whole run was killed (every later app skipped), a failed run had already
deleted a copy's only dump, and nothing alerted because no check ran.
"""
import time

from skbackup_support import FakeHost

DAY = 86400


def shim(host: FakeHost, name: str, body: str) -> None:
    """Put a command double first on the engine's PATH."""
    p = host.bin / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(0o755)


def failing_rsync_for(host: FakeHost, app: str, action: str = "exit 23") -> None:
    """rsync that fails (or hangs) for one app's source and is real otherwise."""
    shim(host, "rsync",
         f'for a in "$@"; do case "$a" in */{app}/) {action};; esac; done\n'
         'exec /usr/bin/rsync "$@"\n')


def old_dailies(host: FakeHost, app: str, n: int) -> None:
    host.zfs("create", "-p", f"backup/copies/{app}")
    for d in range(n, 0, -1):
        host.zfs("snapshot", f"backup/copies/{app}@daily-old{d}", now=int(time.time()) - d * DAY)


def test_retention_prune_runs_when_the_copy_fails(tmp_path):
    host = FakeHost(tmp_path, apps=["app1 app1 2 0 0", "app2 app2 7 4 3"]).seed()
    old_dailies(host, "app1", 4)
    failing_rsync_for(host, "app1")
    proc = host.run("sync")
    assert proc.returncode == 1 and "FAIL app1: rsync exit 23" in proc.stdout
    daily = [n for n in host.snaps("backup/copies/app1") if n.startswith("daily-")]
    assert daily == ["daily-old2", "daily-old1"], "a failed copy must still apply retention"
    assert (host.copies / "app2/c.txt").exists(), "the apps after a failed one are still copied"


def test_retention_prune_runs_when_the_source_is_missing(tmp_path):
    host = FakeHost(tmp_path, apps=["gone gone 1 0 0", "app2 app2 7 4 3"]).seed()
    old_dailies(host, "gone", 3)
    proc = host.run("sync")
    assert "FAIL gone: source gone missing" in proc.stdout
    assert host.snaps("backup/copies/gone") == ["daily-old1"]


def test_copy_deletes_after_the_transfer(tmp_path):
    host = FakeHost(tmp_path).seed()
    args = tmp_path / "rsync-args"
    shim(host, "rsync", f'printf "%s\\n" "$@" >> {args}\nexec /usr/bin/rsync "$@"\n')
    host.run("sync", check=True)
    seen = args.read_text().splitlines()
    assert "--delete-after" in seen and "--delete" not in seen, \
        "deleting before the transfer empties a copy when the pool fills mid-run"


def test_one_hung_app_times_out_and_the_rest_still_copy(tmp_path):
    host = FakeHost(tmp_path, COPY_TIMEOUT_S=2).seed()
    failing_rsync_for(host, "app1", "exec sleep 60")
    t0 = time.time()
    proc = host.run("sync")
    assert time.time() - t0 < 45
    assert proc.returncode == 1 and "FAIL app1: copy timed out after 2s" in proc.stdout
    assert (host.copies / "app2/c.txt").exists()
    assert "done: 1 app(s) copied, fail=1" in proc.stdout


def test_sync_alerts_when_the_backup_pool_is_nearly_full(tmp_path):
    host = FakeHost(tmp_path, SYNC_CAP_WARN=85).seed()
    host.set_pool("backup", cap="91")
    proc = host.run("sync", check=True)
    assert "WARN backup pool backup is 91% full" in proc.stdout
    assert "warn skbackup-sync-pool-backup" in host.alerts()


def test_sync_is_quiet_below_the_precheck(tmp_path):
    host = FakeHost(tmp_path, SYNC_CAP_WARN=85).seed()
    host.set_pool("backup", cap="84")
    host.run("sync", check=True)
    assert host.alerts() == ""


def test_check_warns_when_a_live_copy_emptied(tmp_path):
    host = FakeHost(tmp_path, COPY_SHRINK_MIN_MIB=0).seed()
    host.sanoid()
    host.run("sync", check=True)
    assert "shrank" not in host.run("check").stdout
    (host.copies / "app1/sub/b.bin").unlink()
    out = host.run("check").stdout
    assert "WARN tier2: app1 live copy shrank" in out


def test_check_warns_when_the_newest_copy_shrank_against_the_previous(tmp_path):
    host = FakeHost(tmp_path, COPY_SHRINK_MIN_MIB=0, apps=["app1 app1 7 4 3"]).seed()
    # On real ZFS .zfs is virtual; in the fake it is a directory rsync would delete.
    shim(host, "rsync", 'exec /usr/bin/rsync --filter="P /.zfs/" "$@"\n')
    host.sanoid()
    host.run("sync", check=True)
    first = host.snaps("backup/copies/app1")[0]
    host.set_creation(f"backup/copies/app1@{first}", int(time.time()) - DAY)
    (host.data / "app1/sub/b.bin").unlink()
    time.sleep(1.1)
    host.run("sync", check=True)
    out = host.run("check").stdout
    assert "WARN tier2: app1 newest copy shrank" in out and first in out


def test_check_ignores_small_copies(tmp_path):
    host = FakeHost(tmp_path).seed()  # default minimum 64 MiB
    host.sanoid()
    host.run("sync", check=True)
    (host.copies / "app1/sub/b.bin").unlink()
    assert "shrank" not in host.run("check").stdout


def test_check_watches_thin_volumes(tmp_path):
    host = FakeHost(tmp_path, THINPOOL="vg/pool", THIN_LVS="vg/backup vg/missing", THINPOOL_WARN=85).seed()
    shim(host, "lvs", 'case "${@: -1}" in vg/pool) echo "  50.10";; vg/backup) echo "  96.80";; esac\n')
    host.sanoid()
    out = host.run("check").stdout
    assert "OK thin pool vg/pool 50%" in out
    assert "WARN thin volume vg/backup data 96%" in out
    assert "WARN thin volume vg/missing not found" in out
