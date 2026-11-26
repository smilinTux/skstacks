"""sknet's exporter host play (exporters.yml) run through REAL Ansible against a
scratch host: the units and env files land in a temp tree (sknet_etc_dir,
sknet_unit_dir), systemctl and apt-get are fakes on PATH that keep their state
in files, and softflowd itself is a stub binary. So the real tasks, templates
and `when` conditions run; only the machine is pretended.

Covers: one unit instance per interface with the collector, v9 and full
tracking, the forced ifindex, the guards (collector must be an IPv4 literal,
public collectors refused, duplicate or low indexes, unknown classes), the
package rules (apt only, allow_package_install), the distro service masked,
stale-instance cleanup, state absent, and idempotency."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "sknet"
PLAY = ROLE / "exporters.yml"
UNIT_SRC = ROLE / "src" / "config" / "sknet" / "softflowd@.service.j2"

FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
st = Path(os.environ["FAKE_STATE"]); unit_dir = Path(os.environ["FAKE_UNIT_DIR"])
for d in ("enabled", "active", "masked", "exists"):
    (st / d).mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
with open(st / "calls.log", "a") as f:
    f.write(" ".join(args) + "\n")
cmd, units = args[0], args[1:]
def flag(kind, u): return st / kind / u
def wants(u): return unit_dir / "multi-user.target.wants" / u
if cmd == "is-enabled":
    u = units[0]
    if flag("masked", u).exists():
        print("masked"); sys.exit(1)
    if flag("enabled", u).exists():
        print("enabled"); sys.exit(0)
    if flag("exists", u).exists():
        print("disabled"); sys.exit(1)
    sys.exit(1)
if cmd == "is-active":
    u = units[0]
    if flag("active", u).exists():
        print("active"); sys.exit(0)
    print("inactive"); sys.exit(3)
for u in units:
    if cmd == "enable":
        flag("enabled", u).touch(); w = wants(u); w.parent.mkdir(parents=True, exist_ok=True); w.touch()
    elif cmd == "disable":
        flag("enabled", u).unlink(missing_ok=True); wants(u).unlink(missing_ok=True)
    elif cmd in ("start", "restart"):
        flag("active", u).touch()
    elif cmd == "stop":
        flag("active", u).unlink(missing_ok=True)
    elif cmd == "mask":
        flag("masked", u).touch()
    elif cmd == "unmask":
        flag("masked", u).unlink(missing_ok=True)
sys.exit(0)
'''

FAKE_APT = '''#!/bin/sh
echo "apt-get $*" >> "$FAKE_STATE/calls.log"
if [ "${FAKE_APT_FAIL:-}" = 1 ]; then echo "E: Unable to locate package" >&2; exit 100; fi
printf '#!/bin/sh\\nexit 0\\n' > "$FAKE_BIN/softflowd"
chmod 755 "$FAKE_BIN/softflowd"
'''

GOOD_IFACES = [
    {"name": "eth0", "index": 101, "class": "lan", "speed_mbps": 10000, "description": "Uplink"},
    {"name": "wg-peer", "index": 102, "class": "mesh"},
]


FAKE_IP = '''#!/bin/sh
# ip -4 route get <addr>: answers with the source address the test asks for
src=$(cat "$FAKE_STATE/route_src" 2>/dev/null || echo 192.0.2.20)
echo "$4 dev eth0 src $src uid 0"
'''


class FakeHost:
    """One exporter host on a scratch tree, run by the real play."""

    def __init__(self, tmp: Path, interfaces=None, installed=True, **hostvars):
        self.root = tmp
        self.bin = tmp / "bin"
        self.state = tmp / "state"
        self.etc = tmp / "etc-sknet"
        self.units = tmp / "units"
        for d in (self.bin, self.state, self.units):
            d.mkdir(parents=True, exist_ok=True)
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("apt-get", FAKE_APT), ("ip", FAKE_IP)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        if installed:
            self.install_binary()
        self.vars = {
            "sknet_collector": "192.0.2.10",
            "sknet_export_address": "192.0.2.20",
            "sknet_interfaces": GOOD_IFACES if interfaces is None else interfaces,
        }
        self.vars.update(hostvars)
        self.vault_dir = tmp / "vaults"
        self.set_vault({})
        self.extra = {"skstacks_vault_dir": str(self.vault_dir)}

    def set_vault(self, sknet: dict | None):
        """The instance vault the play reads (None: no vault file at all)."""
        f = self.vault_dir / "optional" / "group_vars" / "prod" / "sknet-prod_vault.yml"
        f.parent.mkdir(parents=True, exist_ok=True)
        if sknet is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(yaml.safe_dump({"sknet": sknet}))

    def install_binary(self, version: str = "1.0.0"):
        # `-h` prints the usage the way the real binary does; 1.1.0 adds -N.
        usage = "  -i [idx:]interface      Specify interface to listen on\n"
        if version != "1.0.0":
            usage += "  -N                      Disable promiscuous mode\n"
        (self.bin / "softflowd").write_text(
            "#!/bin/sh\nif [ \"$1\" = -h ]; then cat <<'EOF'\nUsage: softflowd [options]\n" + usage + "EOF\nfi\nexit 0\n")
        (self.bin / "softflowd").chmod(0o755)

    def inventory(self) -> Path:
        hv = dict(self.vars, ansible_connection="local", ansible_python_interpreter=sys.executable,
                  ansible_become=False, sknet_etc_dir=str(self.etc), sknet_unit_dir=str(self.units))
        inv = tmp_inv = self.root / "inventory.yml"
        tmp_inv.write_text(yaml.safe_dump({"all": {"children": {"sknet_exporter_hosts": {"hosts": {"exp1": hv}}}}}))
        return inv

    def run(self, _env=None, **extra_vars) -> dict:
        env = dict(os.environ, **(_env or {}), PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_STATE=str(self.state),
                   FAKE_UNIT_DIR=str(self.units), FAKE_BIN=str(self.bin),
                   ANSIBLE_LOCAL_TEMP=str(self.root / ".ansible_tmp"), ANSIBLE_HOST_KEY_CHECKING="False",
                   ANSIBLE_RETRY_FILES_ENABLED="False", ANSIBLE_DEPRECATION_WARNINGS="False",
                   ANSIBLE_NOCOLOR="1", ANSIBLE_FORCE_COLOR="0")
        ev = {"ansible_pkg_mgr": "apt", **self.extra, **extra_vars}
        cmd = ["ansible-playbook", "-i", str(self.inventory()), "-e", json.dumps(ev), str(PLAY)]
        proc = subprocess.run(cmd, cwd=PLAY.parent, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        m = re.search(r"exp1\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", proc.stdout)
        return {"rc": proc.returncode, "log": proc.stdout, "changed": int(m.group(2)) if m else None}

    def ok(self, **extra_vars) -> dict:
        res = self.run(**extra_vars)
        assert res["rc"] == 0, res["log"][-5000:]
        return res

    def refused(self, **extra_vars) -> str:
        res = self.run(**extra_vars)
        assert res["rc"] != 0, "the play ran although it must refuse this input:\n" + res["log"][-2000:]
        return res["log"]

    # what the fakes recorded
    def calls(self) -> list[str]:
        f = self.state / "calls.log"
        return f.read_text().splitlines() if f.exists() else []

    def flag(self, kind: str, unit: str) -> bool:
        return (self.state / kind / unit).exists()

    def env_files(self) -> list[str]:
        return sorted(p.name for p in self.etc.glob("softflowd-*.env")) if self.etc.exists() else []

    def env(self, iface: str) -> dict:
        out = {}
        for line in (self.etc / f"softflowd-{iface}.env").read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k] = v.strip("'\"")
        return out

    def unit_text(self) -> str:
        return (self.units / "sknet-softflowd@.service").read_text()


@pytest.fixture
def host(tmp_path):
    return FakeHost(tmp_path)


def u(iface):
    return f"sknet-softflowd@{iface}.service"


# --- the play's shape -------------------------------------------------------

def test_play_targets_the_exporter_group_and_is_privileged():
    play = yaml.safe_load(PLAY.read_text())[0]
    assert play["hosts"] == "{{ sknet_exporter_hosts_group | default('sknet_exporter_hosts') }}"
    assert play["become"] is True


def test_no_dashes_in_the_new_files():
    for p in (PLAY, UNIT_SRC, ROLE / "tasks" / "exporter_guards.yml", ROLE / "tasks" / "exporter_check.yml"):
        text = p.read_text()
        assert chr(0x2014) not in text and chr(0x2013) not in text, p


# --- happy path -------------------------------------------------------------

def test_one_instance_per_interface_with_collector_v9_and_full_tracking(host):
    host.ok()
    unit = host.unit_text()
    assert "-n ${SKNET_COLLECTOR}:${SKNET_PORT}" in unit and "-v 9 -T full" in unit
    assert "-i ${SKNET_IFINDEX}:${SKNET_IFACE}" in unit
    assert host.env_files() == ["softflowd-eth0.env", "softflowd-wg-peer.env"]
    e = host.env("eth0")
    assert (e["SKNET_IFACE"], e["SKNET_IFINDEX"], e["SKNET_COLLECTOR"], e["SKNET_PORT"]) == ("eth0", "101", "192.0.2.10", "2055")
    e = host.env("wg-peer")
    assert (e["SKNET_IFACE"], e["SKNET_IFINDEX"]) == ("wg-peer", "102")
    for i in ("eth0", "wg-peer"):
        assert host.flag("enabled", u(i)) and host.flag("active", u(i))


def test_unit_has_modest_limits_restart_and_env_file_per_instance(host):
    host.ok()
    unit = host.unit_text()
    assert "Restart=on-failure" in unit
    assert re.search(r"^MemoryMax=\d+M$", unit, re.M) and re.search(r"^CPUQuota=\d+%$", unit, re.M)
    assert "EnvironmentFile=" + str(host.etc) + "/softflowd-%i.env" in unit
    assert "ExecStart=" + str(host.bin / "softflowd") + " " in unit


def test_softflowd_1_0_gets_no_dash_n(host):
    """Ubuntu 22.04's softflowd 1.0.0 rejects -N and exits (nor, 2026-11-26)."""
    host.ok()
    exec_line = next(l for l in host.unit_text().splitlines() if l.startswith("ExecStart="))
    assert " -N " not in exec_line and " -T full -p " in exec_line


def test_softflowd_1_1_gets_dash_n(host):
    host.install_binary("1.1.0")
    host.ok()
    exec_line = next(l for l in host.unit_text().splitlines() if l.startswith("ExecStart="))
    assert " -T full -N -p " in exec_line


def test_netflow_port_comes_from_the_vault(host):
    host.ok()
    assert host.env("eth0")["SKNET_PORT"] == "2055"
    host.set_vault({"NETFLOW_PORT": 2056})
    host.ok()
    assert host.env("eth0")["SKNET_PORT"] == "2056" and "2056" in host.env("eth0")["SKNET_BPF"]
    assert f"restart {u('eth0')}" in host.calls()


def test_hostvar_port_equal_to_the_vault_is_accepted(tmp_path):
    h = FakeHost(tmp_path, sknet_netflow_port=2056)
    h.set_vault({"NETFLOW_PORT": 2056})
    h.ok()
    assert h.env("eth0")["SKNET_PORT"] == "2056"


def test_hostvar_port_that_differs_from_the_vault_is_refused(tmp_path):
    h = FakeHost(tmp_path, sknet_netflow_port=2057)
    h.set_vault({"NETFLOW_PORT": 2056})
    log = h.refused()
    assert "2057" in log and "2056" in log and "NETFLOW_PORT" in log
    assert h.env_files() == []
    # the default port counts too: a vault without NETFLOW_PORT means 2055
    h2 = FakeHost(tmp_path / "b", sknet_netflow_port=2056)
    assert "2055" in h2.refused()


def test_missing_vault_is_refused_for_present_but_not_for_absent(tmp_path):
    h = FakeHost(tmp_path)
    h.set_vault(None)
    assert "no sknet vault" in h.refused()
    assert h.env_files() == []
    h2 = FakeHost(tmp_path / "b", sknet_state="absent")
    h2.set_vault(None)
    h2.ok()


def test_environment_selects_the_vault(host):
    # -e env=staging reads sknet-staging_vault.yml
    f = host.vault_dir / "optional" / "group_vars" / "staging" / "sknet-staging_vault.yml"
    f.parent.mkdir(parents=True)
    f.write_text(yaml.safe_dump({"sknet": {"NETFLOW_PORT": 2060}}))
    host.ok(env="staging")
    assert host.env("eth0")["SKNET_PORT"] == "2060"


def test_each_instance_has_its_own_runtime_directory(host):
    # a shared RuntimeDirectory is removed when ANY instance stops, taking the others' pid and ctl files
    host.ok()
    unit = host.unit_text()
    assert "RuntimeDirectory=sknet-softflowd-%i\n" in unit
    assert "-p /run/sknet-softflowd-%i/softflowd.pid" in unit and "-c /run/sknet-softflowd-%i/softflowd.ctl" in unit
    assert "/run/sknet-softflowd/" not in unit and "RuntimeDirectory=sknet-softflowd\n" not in unit


def test_export_traffic_is_excluded_from_the_flows(host):
    host.ok()
    bpf = host.env("eth0")["SKNET_BPF"]
    assert "192.0.2.10" in bpf and "2055" in bpf and bpf.startswith("not ")


def test_distro_service_is_masked_so_it_never_double_exports(host):
    (host.state / "exists").mkdir(parents=True)
    (host.state / "exists" / "softflowd.service").touch()
    (host.state / "enabled").mkdir(parents=True)
    (host.state / "enabled" / "softflowd.service").touch()
    (host.state / "active").mkdir(parents=True)
    (host.state / "active" / "softflowd.service").touch()
    host.ok()
    assert host.flag("masked", "softflowd.service")
    assert not host.flag("enabled", "softflowd.service") and not host.flag("active", "softflowd.service")


def test_no_distro_service_means_nothing_to_mask(host):
    host.ok()
    touched = [c for c in host.calls() if c.split()[0] in ("mask", "stop", "disable", "unmask") and c.endswith(" softflowd.service")]
    assert touched == [] and not (host.etc / ".distro-softflowd-masked").exists()


# --- package rules ----------------------------------------------------------

def test_missing_package_is_installed_with_apt(tmp_path):
    h = FakeHost(tmp_path, installed=False)
    h.ok()
    assert any(c.startswith("apt-get") and "install" in c and "softflowd" in c for c in h.calls())
    assert h.flag("active", u("eth0"))


def test_installed_package_is_not_reinstalled(host):
    host.ok()
    assert not any(c.startswith("apt-get") for c in host.calls())


def test_other_package_manager_fails_clearly_when_the_package_is_missing(tmp_path):
    h = FakeHost(tmp_path, installed=False)
    log = h.refused(ansible_pkg_mgr="pacman")
    assert "softflowd" in log and "apt" in log and "pacman" in log
    assert h.env_files() == []


def test_other_package_manager_is_fine_when_softflowd_is_already_there(tmp_path):
    h = FakeHost(tmp_path)
    h.ok(ansible_pkg_mgr="pacman")
    assert h.flag("active", u("eth0"))


def test_install_disallowed_fails_clearly_when_the_package_is_missing(tmp_path):
    h = FakeHost(tmp_path, installed=False)
    log = h.refused(allow_package_install=False)
    assert "allow_package_install" in log and "softflowd" in log
    assert not any(c.startswith("apt-get") for c in h.calls())


def test_install_disallowed_is_fine_when_the_package_is_there(host):
    host.ok(allow_package_install=False)
    assert host.flag("active", u("eth0"))


def test_failed_apt_install_fails_the_play(tmp_path):
    h = FakeHost(tmp_path, installed=False)
    res = h.run(_env={"FAKE_APT_FAIL": "1"})
    assert res["rc"] != 0 and h.env_files() == []


# --- guards -----------------------------------------------------------------

def test_collector_name_is_refused(tmp_path):
    h = FakeHost(tmp_path, sknet_collector="flows.example.com")
    log = h.refused()
    assert "IPv4 literal" in log and "flows.example.com" in log
    assert h.env_files() == [] and not (h.units / "sknet-softflowd@.service").exists()


@pytest.mark.parametrize("bad", ["", "192.0.2", "192.0.2.300", "2001:db8::1", "192.0.2.10:2055", "192.0.2.0/24"])
def test_collector_that_is_not_an_ipv4_literal_is_refused(tmp_path, bad):
    h = FakeHost(tmp_path, sknet_collector=bad)
    assert "IPv4 literal" in h.refused()


def test_public_collector_is_refused(tmp_path):
    h = FakeHost(tmp_path, sknet_collector="8.8.8.8")
    log = h.refused()
    assert "public" in log and "sknet_allow_public_collector" in log
    assert h.env_files() == []


@pytest.mark.parametrize("ok", ["10.1.2.3", "172.16.0.9", "172.31.255.1", "192.168.5.5", "127.0.0.1",
                                "192.0.2.10", "198.51.100.4", "203.0.113.9"])
def test_private_and_documentation_collectors_pass(tmp_path, ok):
    h = FakeHost(tmp_path, sknet_collector=ok)
    h.ok()
    assert h.env("eth0")["SKNET_COLLECTOR"] == ok


@pytest.mark.parametrize("pub", ["172.32.0.1", "100.128.0.1", "11.0.0.1", "0.0.0.0", "224.0.0.1"])
def test_range_edges_just_outside_private_are_public(tmp_path, pub):
    assert "public" in FakeHost(tmp_path, sknet_collector=pub).refused()


def test_public_collector_allowed_with_the_explicit_override(tmp_path):
    h = FakeHost(tmp_path, sknet_collector="8.8.8.8", sknet_allow_public_collector=True)
    h.ok()
    assert h.env("eth0")["SKNET_COLLECTOR"] == "8.8.8.8"


def test_duplicate_index_is_refused(tmp_path):
    ifs = [{"name": "eth0", "index": 101, "class": "lan"}, {"name": "wt0", "index": 101, "class": "mesh"}]
    h = FakeHost(tmp_path, interfaces=ifs)
    log = h.refused()
    assert "101" in log and "twice" in log
    assert h.env_files() == []


@pytest.mark.parametrize("idx", [0, 99, "x", "", 65536])
def test_index_below_100_or_out_of_range_is_refused(tmp_path, idx):
    h = FakeHost(tmp_path, interfaces=[{"name": "eth0", "index": idx, "class": "lan"}])
    assert "index" in h.refused()


def test_index_at_the_edges_passes(tmp_path):
    h = FakeHost(tmp_path, interfaces=[{"name": "eth0", "index": 100, "class": "lan"},
                                       {"name": "eth1", "index": 65535, "class": "lan"}])
    h.ok()
    assert h.env("eth1")["SKNET_IFINDEX"] == "65535"


def test_unknown_class_is_refused(tmp_path):
    h = FakeHost(tmp_path, interfaces=[{"name": "eth0", "index": 101, "class": "dmz"}])
    log = h.refused()
    assert "class" in log and "dmz" in log


def test_empty_interface_list_and_bad_export_address_are_refused(tmp_path):
    assert "sknet_interfaces" in FakeHost(tmp_path, interfaces=[]).refused()
    assert "sknet_export_address" in FakeHost(tmp_path / "b", sknet_export_address="gw.example.com").refused()


@pytest.mark.parametrize("name", ["eth0:1", "a@b", "has space", "x" * 16])
def test_interface_name_softflowd_and_systemd_cannot_carry_is_refused(tmp_path, name):
    h = FakeHost(tmp_path, interfaces=[{"name": name, "index": 101, "class": "lan"}])
    h.refused()
    assert h.env_files() == []


def test_unknown_state_is_refused(tmp_path):
    assert "sknet_state" in FakeHost(tmp_path, sknet_state="paused").refused()


# --- stale instances --------------------------------------------------------

def test_instance_not_in_the_interface_list_is_stopped_disabled_and_removed(host):
    host.ok()
    assert host.flag("active", u("wg-peer"))
    host.vars["sknet_interfaces"] = [GOOD_IFACES[0]]
    host.ok()
    assert not host.flag("active", u("wg-peer")) and not host.flag("enabled", u("wg-peer"))
    assert host.env_files() == ["softflowd-eth0.env"]
    assert host.flag("active", u("eth0")) and host.flag("enabled", u("eth0"))
    assert f"stop {u('wg-peer')}" in host.calls() and f"disable {u('wg-peer')}" in host.calls()


def test_stale_instance_without_an_env_file_is_still_stopped(host):
    # enabled on the host (the wants symlink) but its env file is already gone
    host.ok()
    (host.etc / "softflowd-wg-peer.env").unlink()
    host.vars["sknet_interfaces"] = [GOOD_IFACES[0]]
    host.ok()
    assert not host.flag("enabled", u("wg-peer")) and not host.flag("active", u("wg-peer"))


def test_stale_cleanup_leaves_the_wanted_instances_running(host):
    host.ok()
    host.vars["sknet_interfaces"] = [GOOD_IFACES[1]]
    host.ok()
    assert host.flag("active", u("wg-peer"))
    assert f"stop {u('eth0')}" in host.calls() and host.env_files() == ["softflowd-wg-peer.env"]


def test_changed_collector_rewrites_the_env_and_restarts_the_instance(host):
    host.ok()
    host.vars["sknet_collector"] = "192.0.2.11"
    host.ok()
    assert host.env("eth0")["SKNET_COLLECTOR"] == "192.0.2.11"
    assert f"restart {u('eth0')}" in host.calls()


# --- state absent -----------------------------------------------------------

def test_absent_stops_disables_and_removes_everything_it_made(host):
    host.ok()
    host.vars["sknet_state"] = "absent"
    res = host.ok()
    for i in ("eth0", "wg-peer"):
        assert not host.flag("active", u(i)) and not host.flag("enabled", u(i))
    assert host.env_files() == []
    assert not (host.units / "sknet-softflowd@.service").exists()
    assert not host.etc.exists()
    assert "left installed" in res["log"]
    assert not any(c.startswith("apt-get") and "remove" in c for c in host.calls())


def test_absent_keeps_a_directory_that_holds_other_files(host):
    host.ok()
    (host.etc / "operator-notes.txt").write_text("keep me\n")
    host.vars["sknet_state"] = "absent"
    host.ok()
    assert (host.etc / "operator-notes.txt").exists() and host.env_files() == []


def test_absent_unmasks_the_distro_service_it_masked(host):
    (host.state / "exists").mkdir(parents=True)
    (host.state / "exists" / "softflowd.service").touch()
    host.ok()
    assert host.flag("masked", "softflowd.service")
    host.vars["sknet_state"] = "absent"
    host.ok()
    assert not host.flag("masked", "softflowd.service")


def test_absent_does_not_need_a_valid_collector_or_the_package(tmp_path):
    h = FakeHost(tmp_path, installed=False, sknet_state="absent", sknet_collector="not-an-ip")
    h.ok()
    assert not any(c.startswith("apt-get") for c in h.calls())


def test_absent_on_a_host_that_never_had_it_changes_nothing(tmp_path):
    h = FakeHost(tmp_path, sknet_state="absent")
    res = h.ok()
    assert res["changed"] == 0


# --- idempotency ------------------------------------------------------------

def test_second_run_reports_no_change(host):
    first = host.ok()
    assert first["changed"] and first["changed"] > 0
    before = len(host.calls())
    second = host.ok()
    assert second["changed"] == 0, second["log"][-3000:]
    new = host.calls()[before:]
    assert not [c for c in new if c.split()[0] in ("enable", "start", "restart", "stop", "disable", "mask")]


def test_second_absent_run_reports_no_change(host):
    host.ok()
    host.vars["sknet_state"] = "absent"
    host.ok()
    assert host.ok()["changed"] == 0


def test_second_run_after_the_distro_service_was_masked_reports_no_change(host):
    (host.state / "exists").mkdir(parents=True)
    (host.state / "exists" / "softflowd.service").touch()
    host.ok()
    assert host.ok()["changed"] == 0


# --- the export address cross-checks (warnings, never failures) --------------

def test_no_warning_when_the_address_is_in_flow_sources_and_is_the_route_source(tmp_path):
    h = FakeHost(tmp_path)
    h.set_vault({"FLOW_SOURCES": ["192.0.2.20"]})
    log = h.ok()["log"]
    assert "is not in FLOW_SOURCES" not in log and "route to the collector" not in log


def test_an_export_address_missing_from_flow_sources_warns_but_runs(tmp_path):
    h = FakeHost(tmp_path)
    h.set_vault({"FLOW_SOURCES": ["192.0.2.99"]})
    log = h.ok()["log"]
    assert "WARNING" in log and "192.0.2.20 is not in FLOW_SOURCES" in log
    assert "softflowd-eth0.env" in h.env_files()


def test_an_empty_flow_sources_also_warns(tmp_path):
    h = FakeHost(tmp_path)
    assert "is not in FLOW_SOURCES" in h.ok()["log"]


def test_a_route_source_that_differs_from_the_export_address_warns_but_runs(tmp_path):
    h = FakeHost(tmp_path)
    h.set_vault({"FLOW_SOURCES": ["192.0.2.20"]})
    (h.state / "route_src").write_text("192.0.2.77")
    log = h.ok()["log"]
    assert "route to the collector" in log and "192.0.2.77" in log and "192.0.2.20" in log
    assert "softflowd-eth0.env" in h.env_files()


def test_no_cross_check_warnings_for_state_absent(tmp_path):
    h = FakeHost(tmp_path, sknet_state="absent")
    log = h.ok()["log"]
    assert "is not in FLOW_SOURCES" not in log and "route to the collector" not in log
