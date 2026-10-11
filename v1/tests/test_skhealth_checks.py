"""skhealth check logic against FAKE command output. Every external command
is a stub that records its argv (skhealth_support.FakeHost), so these tests
pin the exact arguments the engine passes (absolute paths, timeouts, the
read-only ssh options), not only how it parses the answers."""
import datetime as dt
import shutil
import ssl
import subprocess
import threading

import pytest

from skhealth_support import FakeHost, load_engine

eng = load_engine()
NOW = 1_760_000_000.0  # fixed clock


@pytest.fixture
def host(tmp_path):
    return FakeHost(tmp_path)


def collect(host, last_run=None):
    problems, ran, disabled = eng.collect(host.cfg, NOW, last_run)
    return problems, ran, disabled


def enable(host, check, **values):
    host.cfg["checks"][check] = {"enabled": True, **values}


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


# --- swarm services ----------------------------------------------------------

def swarm_manager(host, node_id="aaa", nodes=None):
    host.on("docker", ["info"], f"active true {node_id}\n")
    rows = nodes if nodes is not None else [("aaa", "node-a", "Ready", "Active", "Leader")]
    host.on("docker", ["node", "ls"], "".join("\t".join(r) + "\n" for r in rows))


def test_replicas_below_desired_and_stuck_at_zero(host):
    swarm_manager(host)
    enable(host, "swarm", crashloop_failed_tasks=3, crashloop_minutes=15, ignore_services=["^ignored_"])
    host.on("docker", ["service", "ls"],
            "id1\tapp_web\treplicated\t1/1\n"
            "id2\tapp_db\treplicated\t0/1\n"
            "id3\tapp_api\treplicated\t2/3 (max 1 per node)\n"
            "id4\tapp_edge\tglobal\t4/4\n"
            "id5\tapp_job\treplicated-job\t0/1 (1/1 completed)\n"
            "id6\tignored_svc\treplicated\t0/1\n"
            "id7\tparked\treplicated\t0/0\n")
    host.on("docker", ["service", "ps"], "")
    problems, ran, _ = collect(host)
    assert problems == {
        "swarm.replicas.app_db": "service app_db 0/1 running (stuck at 0)",
        "swarm.replicas.app_api": "service app_api 2/3 running",
    }
    assert "swarm" in ran
    ls = [c for c in host.calls("docker") if c[1:3] == ["service", "ls"]]
    assert ls == [["docker", "service", "ls", "--format", "{{.ID}}\t{{.Name}}\t{{.Mode}}\t{{.Replicas}}"]]


def test_crash_loop_counts_failed_and_rejected_tasks_inside_the_window(host):
    swarm_manager(host)
    enable(host, "swarm", crashloop_failed_tasks=3, crashloop_minutes=15)
    host.on("docker", ["service", "ls"], "s1\tapp_db\treplicated\t1/1\ns2\tapp_web\treplicated\t1/1\n")
    host.on("docker", ["service", "ps"], "t1\nt2\nt3\nt4\nt5\nt6\n")
    host.on("docker", ["inspect"],
            f"s1\tfailed\t{iso(NOW - 60)}\n"
            f"s1\trejected\t{iso(NOW - 300)}\n"
            f"s1\tfailed\t{iso(NOW - 600)}\n"
            f"s2\tfailed\t{iso(NOW - 60)}\n"
            f"s2\tfailed\t{iso(NOW - 3600)}\n"        # outside the window
            f"s2\tshutdown\t{iso(NOW - 30)}\n")       # a normal update, not a failure
    problems, _, _ = collect(host)
    assert problems == {"swarm.crashloop.app_db": "service app_db crash-looping: 3 failed tasks in 15 min"}
    ps = [c for c in host.calls("docker") if c[1:3] == ["service", "ps"]][0]
    assert ps[:6] == ["docker", "service", "ps", "-q", "--filter", "desired-state=shutdown"]
    assert sorted(ps[6:]) == ["app_db", "app_web"]
    insp = [c for c in host.calls("docker") if c[1] == "inspect"][0]
    assert insp[2:4] == ["--format", "{{.ServiceID}}\t{{.Status.State}}\t{{.Status.Timestamp}}"]
    assert sorted(insp[4:]) == ["t1", "t2", "t3", "t4", "t5", "t6"]


def test_nodes_not_ready_drained_or_unreachable(host):
    swarm_manager(host, nodes=[("aaa", "node-a", "Ready", "Active", "Leader"),
                               ("bbb", "node-b", "Down", "Active", "Unreachable"),
                               ("ccc", "node-c", "Ready", "Drain", ""),
                               ("ddd", "node-d", "Ready", "Active", "Reachable"),
                               ("eee", "node-e", "Down", "Active", "")])
    enable(host, "nodes", ignore_nodes=["node-e"])
    problems, _, _ = collect(host)
    assert problems == {
        "swarm.node.node-b": "node node-b: status Down, manager unreachable",
        "swarm.node.node-c": "node node-c: availability Drain",
    }


def test_auto_primary_is_the_lowest_id_ready_manager(host):
    host.cfg["primary"] = "auto"
    enable(host, "swarm")
    host.on("docker", ["service", "ls"], "s1\tapp\treplicated\t0/1\n")
    host.on("docker", ["service", "ps"], "")
    # this node (bbb) is a Ready manager, but aaa is too and sorts first
    swarm_manager(host, node_id="bbb", nodes=[("bbb", "node-b", "Ready", "Active", "Leader"),
                                              ("aaa", "node-a", "Ready", "Active", "Reachable")])
    problems, ran, _ = collect(host)
    assert "swarm" not in ran and not [c for c in host.calls("docker") if c[1:3] == ["service", "ls"]]
    # aaa goes Down: bbb is now the lowest Ready manager and runs the checks
    host.clear("docker")
    swarm_manager(host, node_id="bbb", nodes=[("bbb", "node-b", "Ready", "Active", "Leader"),
                                              ("aaa", "node-a", "Down", "Active", "Unreachable")])
    host.on("docker", ["service", "ls"], "s1\tapp\treplicated\t0/1\n")
    host.on("docker", ["service", "ps"], "")
    problems, ran, _ = collect(host)
    assert "swarm" in ran and "swarm.replicas.app" in problems


def test_a_worker_never_runs_cluster_wide_checks(host):
    host.cfg["primary"] = "auto"
    enable(host, "swarm")
    host.on("docker", ["info"], "active false wkr1\n")
    problems, ran, _ = collect(host)
    assert "swarm" not in ran
    assert [c[1] for c in host.calls("docker")] == ["info"]


# --- host ----------------------------------------------------------------------

def stat_f(host, path, blocks, free, avail, rc=0, out=None):
    host.on("timeout", ["-k", "2", "5", str(host.bin / "stat"), "-f", "-c", "%b %f %a", path],
            out if out is not None else f"{blocks} {free} {avail}\n", rc)


def test_filesystem_over_threshold(host):
    host.cfg["checks"]["host"].update(fs_paths=["/", "/srv/data", "/nope"], fs_max_pct=85,
                                      nfs_timeout_seconds=5)
    stat_f(host, "/", 1000, 100, 50)          # 900 / (900+50) = 95%
    stat_f(host, "/srv/data", 1000, 500, 450)  # 50 / ... = 52%
    stat_f(host, "/nope", 0, 0, 0, rc=1, out="stat: cannot read file system information for '/nope': No such file or directory\n")
    problems, _, _ = collect(host)
    assert problems == {"host.fs./": "filesystem / 95% full (limit 85%)"}
    t = host.calls("timeout")
    assert ["timeout", "-k", "2", "5", str(host.bin / "stat"), "-f", "-c", "%b %f %a", "/"] in t


def test_hung_nfs_mount_alerts_and_is_not_statted_again(host):
    host.cfg["checks"]["host"].update(fs_paths=[], nfs=True, nfs_usage=True, nfs_timeout_seconds=5)
    (host.proc / "mounts").write_text(
        "/dev/vda1 / ext4 rw 0 0\n"
        "nas:/export/data /var/data nfs4 rw 0 0\n"
        "nas:/export/media /mnt/media nfs rw 0 0\n")
    host.on("timeout", ["-k", "2", "5", str(host.bin / "stat"), "-t", "/var/data"], "", 124)
    host.on("timeout", ["-k", "2", "5", str(host.bin / "stat"), "-t", "/mnt/media"], "x\n", 0)
    stat_f(host, "/mnt/media", 1000, 50, 50)  # 95% full
    problems, _, _ = collect(host)
    assert problems == {
        "host.nfs./var/data": "NFS mount /var/data not responding (stat timed out after 5s)",
        "host.fs./mnt/media": "filesystem /mnt/media 95% full (limit 85%)",
    }
    calls = host.calls("timeout")
    assert not [c for c in calls if c[-1] == "/var/data" and "-f" in c], "a hung mount must not be statted twice"


def test_load_memory_and_pressure(host):
    (host.proc / "loadavg").write_text("9.00 5.00 1.00 1/100 1\n")   # 5-min 5.0 over 2 CPUs
    (host.proc / "meminfo").write_text("MemTotal: 1000000 kB\nMemFree: 1 kB\nMemAvailable: 50000 kB\n")
    (host.proc / "pressure" / "memory").write_text(
        "some avg10=50.00 avg60=40.00 avg300=1.00 total=1\nfull avg10=30.00 avg60=25.50 avg300=1.00 total=1\n")
    problems, _, _ = collect(host)
    assert problems["host.load"] == "load 5.0 over 2 CPUs (limit 2 per CPU)"
    assert problems["host.memory"] == "memory 5% available (limit 10%)"
    assert problems["host.memory_pressure"].startswith("memory pressure: tasks fully stalled 25.5%")


def test_failed_units_with_ignore_globs(host):
    host.cfg["checks"]["host"]["ignore_units"] = ["apt-daily*"]
    host.on("systemctl", ["--failed"], "nginx.service loaded failed failed A web server\n"
                                       "apt-daily.service loaded failed failed Daily apt\n")
    problems, _, _ = collect(host)
    assert problems == {"host.unit.nginx.service": "systemd unit nginx.service failed"}
    assert host.calls("systemctl") == [["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"]]


def test_oom_since_the_last_run(host):
    host.cfg["checks"]["host"]["oom"] = True
    host.on("journalctl", ["-k"], "Out of memory: Killed process 4242 (postgres) total-vm:1kB\n"
                                  "Out of memory: Killed process 4343 (java) total-vm:1kB\n")
    problems, _, _ = collect(host, last_run=NOW - 300)
    assert problems == {"host.oom": "OOM killer fired 2 time(s) since the last run: java, postgres"}
    assert host.calls("journalctl") == [["journalctl", "-k", "--since", f"@{int(NOW - 300)}",
                                         "--no-pager", "-q", "-o", "cat"]]
    host.reset_calls()
    collect(host, last_run=None)  # first run: the last 10 minutes
    assert host.calls("journalctl")[0][3] == f"@{int(NOW - 600)}"


def test_missing_tools_skip_their_check_instead_of_failing(host):
    host.cfg["bin"]["systemctl"] = str(host.root / "absent" / "systemctl")
    host.cfg["bin"]["smartctl"] = str(host.root / "absent" / "smartctl")
    enable(host, "disks", nvme_used_max_pct=90)
    problems, ran, _ = collect(host)
    assert problems == {} and "disks" not in ran and "host" in ran


# --- disks and arrays ------------------------------------------------------------

SMART_NVME_BAD = """=== START OF SMART DATA SECTION ===
SMART overall-health self-assessment test result: PASSED
Critical Warning:                   0x04
Percentage Used:                    97%
"""
SMART_SATA_FAILED = "SMART overall-health self-assessment test result: FAILED!\n"
SMART_OK = "SMART overall-health self-assessment test result: PASSED\nPercentage Used: 3%\nCritical Warning: 0x00\n"
SMART_SAS_BAD = "SMART Health Status: FIRMWARE IMPENDING FAILURE\n"


def test_smart_health_and_nvme_wear(host):
    enable(host, "disks", nvme_used_max_pct=90)
    host.on("smartctl", ["--scan"], "/dev/nvme0 -d nvme # nvme0\n/dev/sda -d sat # sda\n"
                                    "/dev/sdb -d sat # sdb\n/dev/sdc -d scsi # sdc\n/dev/sdd -d sat # asleep\n")
    host.on("smartctl", ["-H", "-A", "-n", "standby", "-d", "nvme", "/dev/nvme0"], SMART_NVME_BAD, 0)
    host.on("smartctl", ["-H", "-A", "-n", "standby", "-d", "sat", "/dev/sda"], SMART_SATA_FAILED, 8)
    host.on("smartctl", ["-H", "-A", "-n", "standby", "-d", "sat", "/dev/sdb"], SMART_OK, 0)
    host.on("smartctl", ["-H", "-A", "-n", "standby", "-d", "scsi", "/dev/sdc"], SMART_SAS_BAD, 0)
    host.on("smartctl", ["-H", "-A", "-n", "standby", "-d", "sat", "/dev/sdd"],
            "Device is in STANDBY mode, exit(2)\n", 2)
    problems, _, _ = collect(host)
    assert problems == {
        "disk./dev/nvme0": "disk /dev/nvme0: NVMe critical warning 0x04; NVMe 97% used (limit 90%)",
        "disk./dev/sda": "disk /dev/sda: SMART health FAILED!",
        "disk./dev/sdc": "disk /dev/sdc: SMART health status FIRMWARE IMPENDING FAILURE",
    }


@pytest.mark.parametrize("text,expected", [
    (SMART_OK, []),
    ("", []),
    ("Percentage Used: 90%\n", ["NVMe 90% used (limit 90%)"]),
    ("Critical Warning: 0x00\nPercentage Used: 89%\n", []),
])
def test_parse_smart_edges(text, expected):
    assert eng.parse_smart(text, 90) == expected


ZPOOL_DEGRADED = """  pool: tank
 state: DEGRADED
status: One or more devices could not be used because the label is missing.
config:
        NAME        STATE     READ WRITE CKSUM
        tank        DEGRADED     0     0     0
          mirror-0  DEGRADED     0     0     0
            sda     ONLINE       0     0     0
            sdb     UNAVAIL      0     0     0
"""
MDADM_DEGRADED = """/dev/md0:
             State : clean, degraded
    Active Devices : 1
    Failed Devices : 1
"""


def test_zfs_degraded_and_md_degraded(host):
    enable(host, "raid")
    (host.proc / "mdstat").write_text("Personalities : [raid1]\nmd0 : active raid1 sda1[0]\nmd1 : active raid1 sdc1[0] sdd1[1]\n")
    host.on("zpool", ["status", "-x"], ZPOOL_DEGRADED)
    host.on("mdadm", ["--detail", "/dev/md0"], MDADM_DEGRADED)
    host.on("mdadm", ["--detail", "/dev/md1"], "/dev/md1:\n             State : clean\n    Failed Devices : 0\n")
    problems, _, _ = collect(host)
    assert problems == {
        "raid.zpool.tank": "ZFS pool tank DEGRADED",
        "raid.md.md0": "RAID /dev/md0: state clean, degraded; 1 failed device(s)",
    }
    assert host.calls("zpool") == [["zpool", "status", "-x"]]


@pytest.mark.parametrize("text", ["all pools are healthy\n", "no pools available\n", ""])
def test_healthy_zpool_outputs(text):
    assert eng.parse_zpool_status(text) == {}


# --- remote read-only hosts --------------------------------------------------------

def remote_host(host, **over):
    rh = {"name": "nas", "host": "nas.example.test", "port": 2222, "user": "skhealth",
          "zpool": True, "smart_devices": ["/dev/nvme0n1"], "pvesm": True} | over
    host.cfg["remote"] = {"hosts": [rh], "key_file": "/etc/skhealth/remote_ed25519",
                          "known_hosts_file": "/etc/skhealth/known_hosts", "strict_host_keys": True}
    host.cfg["checks"]["disks"] = {"enabled": False, "nvme_used_max_pct": 90}
    return rh


def ssh_prefix(host):
    return ["-n", "-T", "-i", "/etc/skhealth/remote_ed25519", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
            "-o", "UserKnownHostsFile=/etc/skhealth/known_hosts", "-o", "StrictHostKeyChecking=yes",
            "-p", "2222", "skhealth@nas.example.test"]


def test_remote_checks_send_only_the_allowlisted_commands(host):
    remote_host(host)
    pre = ssh_prefix(host)
    host.on("ssh", pre + ["zpool status -x"], ZPOOL_DEGRADED)
    host.on("ssh", pre + ["smartctl -H /dev/nvme0n1"], "SMART overall-health self-assessment test result: PASSED\n", 0)
    host.on("ssh", pre + ["smartctl -A /dev/nvme0n1"], "Critical Warning: 0x01\nPercentage Used: 100%\n", 0)
    host.on("ssh", pre + ["pvesm status"],
            "Name Type Status Total Used Available %\n"
            "local dir active 100 50 50 50.00%\n"
            "tank zfspool active 1000 900 100 90.00%\n"
            "nfsbak nfs inactive 0 0 0 0.00%\n"
            "old dir disabled 0 0 0 N/A\n")
    problems, ran, _ = collect(host)
    assert "remote" in ran
    assert problems == {
        "remote.nas.zpool.tank": "remote nas: ZFS pool tank DEGRADED",
        "remote.nas.disk./dev/nvme0n1": "remote nas: disk /dev/nvme0n1: NVMe critical warning 0x01; NVMe 100% used (limit 90%)",
        "remote.nas.pvesm.tank": "remote nas: tank storage 90% full (limit 85%)",
        "remote.nas.pvesm.nfsbak": "remote nas: nfsbak storage inactive",
    }
    sent = [c[len(pre) + 1:] for c in host.calls("ssh")]
    assert sent == [["zpool status -x"], ["smartctl -H /dev/nvme0n1"], ["smartctl -A /dev/nvme0n1"], ["pvesm status"]]
    assert all(c[1:len(pre) + 1] == pre for c in host.calls("ssh"))


def test_remote_unreachable_is_one_problem_and_stops_that_host(host):
    remote_host(host)
    host.on("ssh", ssh_prefix(host) + ["zpool status -x"], "ssh: connect to host nas.example.test port 2222: Connection refused\n", 255)
    problems, _, _ = collect(host)
    assert problems == {"remote.nas.ssh": "remote nas: ssh failed: ssh: connect to host nas.example.test port 2222: Connection refused"}
    assert len(host.calls("ssh")) == 1


def test_remote_denied_by_the_wrapper(host):
    remote_host(host, zpool=False, pvesm=True, smart_devices=[])
    host.on("ssh", ssh_prefix(host) + ["pvesm status"], "skhealth-remote: denied\n", 126)
    problems, _, _ = collect(host)
    assert problems == {"remote.nas.denied": "remote nas: the forced-command wrapper denied 'pvesm status'"}


def test_remote_host_keys_accept_new_without_known_hosts(host):
    remote_host(host)
    host.cfg["remote"]["strict_host_keys"] = False
    collect(host)
    assert "StrictHostKeyChecking=accept-new" in host.calls("ssh")[0]


# --- TLS ------------------------------------------------------------------------------

def make_cert(tmp_path, days):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(crt), "-days", str(days), "-subj", "/CN=tls.example.test"],
                   check=True, capture_output=True)
    return key, crt


def tls_server(key, crt):
    import socket
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                with ctx.wrap_socket(conn, server_side=True) as s:
                    s.recv(1)
            except (OSError, ssl.SSLError):
                pass

    threading.Thread(target=serve, daemon=True).start()
    return srv


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_tls_expiry_under_threshold(tmp_path):
    host = FakeHost(tmp_path / "h")
    key, crt = make_cert(tmp_path, 5)
    srv = tls_server(key, crt)
    port = srv.getsockname()[1]
    try:
        host.cfg["checks"]["tls"] = {"hosts": [{"host": "tls.example.test", "port": port, "address": "127.0.0.1"},
                                               f"127.0.0.1:{port}"],
                                     "warn_days": 21}
        problems, _, _ = eng.collect(host.cfg, __import__("time").time(), None)
        assert problems[f"tls.tls.example.test:{port}"].startswith(f"TLS tls.example.test:{port} certificate expires in")
        assert f"tls.127.0.0.1:{port}" in problems
        host.cfg["checks"]["tls"]["warn_days"] = 2
        problems, _, _ = eng.collect(host.cfg, __import__("time").time(), None)
        assert problems == {}
    finally:
        srv.close()


def test_tls_unreachable_is_a_problem(host):
    host.cfg["checks"]["tls"] = {"hosts": ["127.0.0.1:1"], "warn_days": 21}
    problems, _, _ = collect(host)
    assert problems == {"tls.127.0.0.1:1": "TLS 127.0.0.1:1: cannot read the certificate (ConnectionRefusedError)"}


# --- one broken check never hides the others ---------------------------------------

def test_a_crashing_check_is_reported_and_the_rest_still_run(host):
    enable(host, "raid")
    (host.proc / "mdstat").write_text("garbage")
    host.cfg["bin"]["zpool"] = str(host.root / "absent")
    host.cfg["bin"]["mdadm"] = str(host.bin / "mdadm")
    (host.proc / "loadavg").unlink()  # the host check will crash reading it
    host.on("systemctl", ["--failed"], "x.service loaded failed failed X\n")
    problems, ran, _ = collect(host)
    assert problems["check.host"].startswith("check host failed: FileNotFoundError")
    assert "raid" in ran
