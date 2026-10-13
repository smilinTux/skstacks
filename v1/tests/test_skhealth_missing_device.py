"""skhealth: a SMART device that is not there is a PROBLEM, never healthy.

Before v2.26.6 a remote `smart_devices` path that did not exist (a wrong
by-id name, a swapped disk) produced smartctl's "No such device" with exit 2,
no health line, and parse_smart() found nothing in it: the disk was reported
healthy. Locally there was no way to name the disks a host must have at all.

Now:
* remote: smartctl answering "No such device"/"No such file", or exit status
  bit 1 (device open failed) without a health line, is "device not found";
  bit 0 (command line or sudo failure) without a health line is "could not
  read it". Both alert.
* local: `disks.devices` (optional) lists the paths a host must have. A
  missing one is "device not found" and smartctl is not run on it; a present
  one that `smartctl --scan` does not list is checked by its path.

Every external command is a stub that records its argv (FakeHost).
"""
import pytest

from skhealth_support import FakeHost, load_engine

eng = load_engine()
NOW = 1_760_000_000.0
BANNER = "smartctl 7.4 2023-08-01 r5530 [x86_64-linux-6.8.0] (local build)\nCopyright (C) 2002-23, Bruce Allen\n\n"
NO_SUCH_DEVICE = BANNER + "Smartctl open device: /dev/disk/by-id/nvme-WRONG failed: No such device\n"
NO_SUCH_FILE = BANNER + "Smartctl open device: /dev/sdz failed: No such file or directory\n"
OPEN_FAILED = BANNER + "/dev/sdb: Unknown USB bridge [0x1234:0x5678 (0x000)]\nPlease specify device type with the -d option.\n"
HEALTH_OK = BANNER + "=== START OF READ SMART DATA SECTION ===\nSMART overall-health self-assessment test result: PASSED\n"
ATTRS_OK = BANNER + "=== START OF READ SMART DATA SECTION ===\nCritical Warning: 0x00\nPercentage Used: 3%\n"


@pytest.fixture
def host(tmp_path):
    return FakeHost(tmp_path)


def collect(host):
    problems, ran, _ = eng.collect(host.cfg, NOW, None)
    return problems, ran


# --- remote ----------------------------------------------------------------

def remote(host, devices):
    host.cfg["remote"] = {"hosts": [{"name": "nas", "host": "nas.example.test", "port": 22, "user": "skhealth",
                                     "zpool": False, "smart_devices": devices, "pvesm": False}],
                          "key_file": "/k", "known_hosts_file": "/kh", "strict_host_keys": True}
    host.cfg["checks"]["disks"] = {"enabled": False, "nvme_used_max_pct": 90}


def ssh_says(host, command, out, rc):
    host.on("ssh", ["-n", "-T", "-i", "/k", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                    "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                    "-o", "UserKnownHostsFile=/kh", "-o", "StrictHostKeyChecking=yes",
                    "-p", "22", "skhealth@nas.example.test", command], out, rc)


def test_remote_no_such_device_is_device_not_found(host):
    dev = "/dev/disk/by-id/nvme-WRONG"
    remote(host, [dev])
    ssh_says(host, f"smartctl -H {dev}", NO_SUCH_DEVICE, 2)
    ssh_says(host, f"smartctl -A {dev}", NO_SUCH_DEVICE, 2)
    problems, ran = collect(host)
    assert "remote" in ran
    assert problems == {f"remote.nas.disk.{dev}":
                        f"remote nas: disk {dev}: device not found (smartctl rc 2): "
                        f"Smartctl open device: {dev} failed: No such device"}
    sent = [c[-1] for c in host.calls("ssh")]
    assert sent == [f"smartctl -H {dev}", f"smartctl -A {dev}"]


def test_remote_no_such_file_text_alone_is_device_not_found(host):
    remote(host, ["/dev/sdz"])
    ssh_says(host, "smartctl -H /dev/sdz", NO_SUCH_FILE, 0)
    ssh_says(host, "smartctl -A /dev/sdz", NO_SUCH_FILE, 0)
    problems, _ = collect(host)
    assert problems == {"remote.nas.disk./dev/sdz": "remote nas: disk /dev/sdz: device not found (smartctl rc 0): "
                                                    "Smartctl open device: /dev/sdz failed: No such file or directory"}


def test_remote_exit_2_without_a_health_line_is_device_not_found(host):
    remote(host, ["/dev/sdb"])
    ssh_says(host, "smartctl -H /dev/sdb", OPEN_FAILED, 2)
    ssh_says(host, "smartctl -A /dev/sdb", OPEN_FAILED, 2)
    problems, _ = collect(host)
    assert list(problems) == ["remote.nas.disk./dev/sdb"]
    assert problems["remote.nas.disk./dev/sdb"].startswith("remote nas: disk /dev/sdb: device not found (smartctl rc 2): ")


def test_remote_sudo_refusal_is_could_not_read(host):
    remote(host, ["/dev/sda"])
    ssh_says(host, "smartctl -H /dev/sda", "sudo: a password is required\n", 1)
    ssh_says(host, "smartctl -A /dev/sda", "sudo: a password is required\n", 1)
    problems, _ = collect(host)
    assert problems == {"remote.nas.disk./dev/sda":
                        "remote nas: disk /dev/sda: smartctl could not read it (rc 1): sudo: a password is required"}


def test_remote_healthy_device_stays_healthy(host):
    remote(host, ["/dev/nvme0n1"])
    ssh_says(host, "smartctl -H /dev/nvme0n1", HEALTH_OK, 0)
    ssh_says(host, "smartctl -A /dev/nvme0n1", ATTRS_OK, 0)
    problems, _ = collect(host)
    assert problems == {}


def test_remote_disk_status_bits_with_a_health_line_are_not_open_failures(host):
    """Exit bits 2+ report disk state, not a failed open: a readable disk with
    bit 2 (some command failed) and a PASSED health line stays healthy."""
    remote(host, ["/dev/sda"])
    ssh_says(host, "smartctl -H /dev/sda", HEALTH_OK, 4)
    ssh_says(host, "smartctl -A /dev/sda", ATTRS_OK, 4)
    problems, _ = collect(host)
    assert problems == {}


def test_remote_one_missing_device_does_not_hide_the_others(host):
    remote(host, ["/dev/nvme0n1", "/dev/sdz"])
    ssh_says(host, "smartctl -H /dev/nvme0n1", HEALTH_OK, 0)
    ssh_says(host, "smartctl -A /dev/nvme0n1", ATTRS_OK.replace("Percentage Used: 3%", "Percentage Used: 95%"), 0)
    ssh_says(host, "smartctl -H /dev/sdz", NO_SUCH_DEVICE, 2)
    ssh_says(host, "smartctl -A /dev/sdz", NO_SUCH_DEVICE, 2)
    problems, _ = collect(host)
    assert set(problems) == {"remote.nas.disk./dev/nvme0n1", "remote.nas.disk./dev/sdz"}
    assert "device not found" in problems["remote.nas.disk./dev/sdz"]


# --- local -----------------------------------------------------------------

def local(host, devices=None, scan=""):
    host.cfg["checks"]["disks"] = {"enabled": True, "nvme_used_max_pct": 90}
    if devices is not None:
        host.cfg["checks"]["disks"]["devices"] = devices
    host.save()
    host.on("smartctl", ["--scan"], scan)


def test_local_missing_listed_device_is_device_not_found_and_not_queried(host, tmp_path):
    missing = str(tmp_path / "dev/disk/by-id/ata-SWAPPED")
    local(host, [missing], scan="/dev/sda -d sat # sda\n")
    host.on("smartctl", ["-H", "-A", "-n", "standby", "-d", "sat", "/dev/sda"], HEALTH_OK, 0)
    problems, ran = collect(host)
    assert "disks" in ran
    assert problems == {f"disk.{missing}": f"disk {missing}: device not found"}
    assert all(missing not in c for c in host.calls("smartctl"))


def test_local_dangling_by_id_symlink_is_device_not_found(host, tmp_path):
    link = tmp_path / "by-id-link"
    link.symlink_to(tmp_path / "gone")
    local(host, [str(link)])
    problems, _ = collect(host)
    assert problems == {f"disk.{link}": f"disk {link}: device not found"}


def test_local_present_listed_device_not_in_scan_is_checked_by_path(host, tmp_path):
    dev = tmp_path / "nvme-present"
    dev.write_text("")
    local(host, [str(dev)], scan="")
    host.on("smartctl", ["-H", "-A", "-n", "standby", str(dev)], HEALTH_OK + "Percentage Used: 99%\n", 0)
    problems, _ = collect(host)
    assert problems == {f"disk.{dev}": f"disk {dev}: NVMe 99% used (limit 90%)"}
    assert ["smartctl", "-H", "-A", "-n", "standby", str(dev)] in host.calls("smartctl")


def test_local_listed_device_that_the_scan_already_covers_is_checked_once(host, tmp_path):
    link = tmp_path / "ata-DISK"
    link.symlink_to("/dev/null")  # a by-id style link to a scanned /dev node
    local(host, [str(link)], scan="/dev/null -d sat # scanned\n")
    host.on("smartctl", ["-H", "-A", "-n", "standby", "-d", "sat", "/dev/null"], HEALTH_OK, 0)
    problems, _ = collect(host)
    assert problems == {}
    queried = [c for c in host.calls("smartctl") if c[1] != "--scan"]
    assert queried == [["smartctl", "-H", "-A", "-n", "standby", "-d", "sat", "/dev/null"]]


def test_local_scanned_device_answering_no_such_device_is_device_not_found(host):
    local(host, scan="/dev/sdb -d sat # sdb\n")
    host.on("smartctl", ["-H", "-A", "-n", "standby", "-d", "sat", "/dev/sdb"],
            BANNER + "Smartctl open device: /dev/sdb [SAT] failed: No such device\n", 2)
    problems, _ = collect(host)
    assert problems == {"disk./dev/sdb": "disk /dev/sdb: device not found (smartctl rc 2): "
                                         "Smartctl open device: /dev/sdb [SAT] failed: No such device"}


def test_local_standby_is_still_skipped(host):
    local(host, scan="/dev/sdd -d sat # asleep\n")
    host.on("smartctl", ["-H", "-A", "-n", "standby", "-d", "sat", "/dev/sdd"], "Device is in STANDBY mode, exit(2)\n", 2)
    problems, _ = collect(host)
    assert problems == {}


def test_local_no_devices_key_keeps_scan_only_behaviour(host):
    local(host, scan="/dev/sda -d sat # sda\n")
    host.on("smartctl", ["-H", "-A", "-n", "standby", "-d", "sat", "/dev/sda"], HEALTH_OK, 0)
    problems, _ = collect(host)
    assert problems == {}
    assert len(host.calls("smartctl")) == 2
