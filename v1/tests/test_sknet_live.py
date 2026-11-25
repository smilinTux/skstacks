"""sknet's live host play (live.yml) run through REAL Ansible against a scratch
host: units, script and data dir land in a temp tree (sknet_unit_dir,
sknet_live_sbin_dir, sknet_live_data_dir); systemctl, docker, iptables and
iptables-restore are fakes on PATH that keep their state in files. So the real
tasks, templates and `when` conditions run; only the machine is pretended.

Covers: the rendered ntopng flags (-i per interface, -w bind:3000, -l 1, -d), the
pinned image, the container and unit memory limits (always rendered), capabilities
instead of --privileged, the bind and source guards, the allowlist chain (run for
real against a fake iptables), the vault, state absent cleanup, and idempotency."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402
from test_sknet_exporters import FAKE_SYSTEMCTL  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "sknet"
PLAY = ROLE / "live.yml"
CFG = ROLE / "src" / "config" / "sknet"
DEFAULTS = yaml.safe_load((ROLE / "vars" / "defaults.yml").read_text())

FAKE_DOCKER = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
st = Path(os.environ["FAKE_STATE"]); (st / "images").mkdir(parents=True, exist_ok=True)
a = sys.argv[1:]
with open(st / "calls.log", "a") as f:
    f.write("docker " + " ".join(a) + "\n")
ctr = st / "container"
if a[:2] == ["image", "inspect"]:
    sys.exit(0 if (st / "images" / a[2].replace("/", "_")).exists() else 1)
if a[0] == "pull":
    (st / "images" / a[1].replace("/", "_")).touch(); sys.exit(0)
if a[0] == "info":
    print("active" if (st / "swarm").exists() else "inactive"); sys.exit(0)
if a[0] == "ps":
    if ctr.exists(): print("abc123")
    sys.exit(0)
if a[0] == "rm":
    ctr.unlink(missing_ok=True); sys.exit(0)
sys.exit(0)
'''

FAKE_IPTABLES = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
st = Path(os.environ["FAKE_STATE"]); st.mkdir(parents=True, exist_ok=True)
a = [x for x in sys.argv[1:] if x != "-w"]
with open(st / "calls.log", "a") as f:
    f.write("iptables " + " ".join(a) + "\n")
hooks = st / "hooks"; hooks.touch()
lines = hooks.read_text().splitlines()
op, rest = a[0], " ".join(a[1:])
if op == "-C":
    sys.exit(0 if rest in lines else 1)
if op in ("-I", "-A"):
    # -I INPUT <spec>: keep the spec after the parent chain name
    lines.append(rest.replace("-I ", "", 1)); hooks.write_text("\n".join(lines) + "\n"); sys.exit(0)
if op == "-D":
    if rest in lines: lines.remove(rest)
    hooks.write_text("\n".join(lines) + "\n"); sys.exit(0)
if op == "-n" and a[1] == "-L":
    sys.exit(0 if (st / ("chain-" + a[2])).exists() else 1)
if op == "-S":
    for l in lines:
        if l.startswith(a[1] + " "): print("-A " + l)
    sys.exit(0)
if op == "-F":
    sys.exit(0)
if op == "-X":
    (st / ("chain-" + a[1])).unlink(missing_ok=True); sys.exit(0)
if op == "-N":
    sys.exit(0)
sys.exit(0)
'''

FAKE_RESTORE = r'''#!/usr/bin/env python3
import os, re, sys
from pathlib import Path
st = Path(os.environ["FAKE_STATE"]); st.mkdir(parents=True, exist_ok=True)
data = sys.stdin.read()
with open(st / "calls.log", "a") as f:
    f.write("iptables-restore " + " ".join(sys.argv[1:]) + "\n")
for m in re.finditer(r"^:(\S+) ", data, re.M):
    (st / ("chain-" + m.group(1))).touch()
    (st / ("rules-" + m.group(1))).write_text("\n".join(l for l in data.splitlines() if l.startswith("-A " + m.group(1))) + "\n")
sys.exit(0)
'''

HOOK = "INPUT -p tcp -d 192.0.2.30 --dport 3000 -j SKNET-LIVE"   # only ntopng's own address, not every tcp 3000
IMG = DEFAULTS["sknet_defaults"]["NTOPNG_IMAGE"]


class LiveHost:
    def __init__(self, tmp: Path, docker=True, **hostvars):
        self.root = tmp
        self.bin = tmp / "bin"
        self.state = tmp / "state"
        self.units = tmp / "units"
        self.sbin = tmp / "sbin"
        self.data = tmp / "data" / "sknet-live"
        for d in (self.bin, self.state, self.units, self.sbin):
            d.mkdir(parents=True, exist_ok=True)
        tools = {"systemctl": FAKE_SYSTEMCTL, "iptables": FAKE_IPTABLES, "iptables-restore": FAKE_RESTORE}
        if docker:
            tools["docker"] = FAKE_DOCKER
        for name, body in tools.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.vars = {
            "sknet_live_interfaces": ["eth0", "wg-peer"],
            "sknet_live_bind": "192.0.2.30",
            "sknet_live_allow_sources": ["192.0.2.50", "198.51.100.0/24"],
        }
        self.vars.update(hostvars)
        self.vault_dir = tmp / "vaults"
        self.set_vault({})
        self.extra = {"skstacks_vault_dir": str(self.vault_dir)}

    def set_vault(self, sknet):
        f = self.vault_dir / "optional" / "group_vars" / "prod" / "sknet-prod_vault.yml"
        f.parent.mkdir(parents=True, exist_ok=True)
        if sknet is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(yaml.safe_dump({"sknet": sknet}))

    def env(self, **more):
        return dict(os.environ, **more, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_STATE=str(self.state),
                    FAKE_UNIT_DIR=str(self.units), ANSIBLE_LOCAL_TEMP=str(self.root / ".ansible_tmp"),
                    ANSIBLE_HOST_KEY_CHECKING="False", ANSIBLE_RETRY_FILES_ENABLED="False",
                    ANSIBLE_DEPRECATION_WARNINGS="False", ANSIBLE_NOCOLOR="1", ANSIBLE_FORCE_COLOR="0")

    def inventory(self) -> Path:
        hv = dict(self.vars, ansible_connection="local", ansible_python_interpreter=sys.executable,
                  ansible_become=False, sknet_unit_dir=str(self.units), sknet_live_sbin_dir=str(self.sbin),
                  sknet_live_data_dir=str(self.data))
        inv = self.root / "inventory.yml"
        inv.write_text(yaml.safe_dump({"all": {"children": {"sknet_live_hosts": {"hosts": {"live1": hv}}}}}))
        return inv

    def run(self, **extra_vars) -> dict:
        ev = {**self.extra, **extra_vars}
        cmd = ["ansible-playbook", "-i", str(self.inventory()), "-e", json.dumps(ev), str(PLAY)]
        proc = subprocess.run(cmd, cwd=PLAY.parent, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        m = re.search(r"live1\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", proc.stdout)
        return {"rc": proc.returncode, "log": proc.stdout, "changed": int(m.group(2)) if m else None}

    def ok(self, **extra_vars):
        res = self.run(**extra_vars)
        assert res["rc"] == 0, res["log"][-5000:]
        return res

    def refused(self, **extra_vars) -> str:
        res = self.run(**extra_vars)
        assert res["rc"] != 0, "the play ran although it must refuse this input:\n" + res["log"][-2000:]
        return res["log"]

    def calls(self):
        f = self.state / "calls.log"
        return f.read_text().splitlines() if f.exists() else []

    def flag(self, kind, unit):
        return (self.state / kind / unit).exists()

    def unit_text(self) -> str:
        return (self.units / "sknet-live.service").read_text()

    def exec_start(self) -> str:
        return next(l for l in self.unit_text().splitlines() if l.startswith("ExecStart="))

    def apply_script(self):
        """Run the rendered allowlist script against the fake iptables."""
        p = subprocess.run(["bash", str(self.sbin / "sknet-live-fw")], env=self.env(), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
        assert p.returncode == 0, p.stdout
        return p

    def rules(self):
        f = self.state / "rules-SKNET-LIVE"
        return f.read_text().splitlines() if f.exists() else []

    def hooks(self):
        f = self.state / "hooks"
        return [l for l in f.read_text().splitlines() if l] if f.exists() else []


@pytest.fixture
def host(tmp_path):
    return LiveHost(tmp_path)


# --- the play's shape ---------------------------------------------------------

def test_play_targets_the_live_group_and_is_privileged():
    play = yaml.safe_load(PLAY.read_text())[0]
    assert play["hosts"] == "{{ sknet_live_hosts_group | default('sknet_live_hosts') }}"
    assert play["become"] is True


def test_no_dashes_in_the_new_files():
    for p in (PLAY, CFG / "sknet-live.service.j2", CFG / "firewall" / "sknet-live-fw.sh.j2",
              CFG / "firewall" / "sknet-live-fw.service.j2", ROLE / "tasks" / "live_guards.yml"):
        text = p.read_text()
        assert chr(0x2014) not in text and chr(0x2013) not in text, p


def test_interface_name_rule_is_shared_with_the_exporters():
    assert "sknet_re_ifname" in (ROLE / "tasks" / "exporter_check.yml").read_text()
    assert "sknet_re_ifname" in (ROLE / "tasks" / "live_guards.yml").read_text()
    assert "A-Za-z0-9_.-]{1,15}" not in (ROLE / "tasks" / "exporter_check.yml").read_text()


# --- happy path ---------------------------------------------------------------

def test_unit_renders_the_ntopng_flags(host):
    host.ok()
    ex = host.exec_start()
    assert " --network host " in ex and "--privileged" not in ex
    assert "--name sknet-live" in ex and " run --rm " in ex
    assert " -i eth0 -i wg-peer " in ex
    assert " -w 192.0.2.30:3000 " in ex
    assert " -l 1 " in ex and " -d /var/lib/ntopng " in ex
    assert f"-v {host.data}/ntopng:/var/lib/ntopng" in ex
    assert " -x 16384 -X 16384" in ex
    assert "ExecStartPre=-" in host.unit_text() and "rm -f sknet-live" in host.unit_text()
    assert "Restart=on-failure" in host.unit_text()


def test_image_is_the_pinned_default_and_never_latest(host):
    host.ok()
    assert f" {IMG} " in host.exec_start()
    assert "@sha256:" in IMG and not IMG.endswith(":latest")
    assert f"docker pull {IMG}" in host.calls()


def test_vault_image_override_is_used_and_latest_is_refused(host):
    host.set_vault({"NTOPNG_IMAGE": "ntop/ntopng:7.0.1"})
    host.ok()
    assert " ntop/ntopng:7.0.1 " in host.exec_start()
    h2 = LiveHost(host.root / "b")
    h2.set_vault({"NTOPNG_IMAGE": "ntop/ntopng:latest"})
    assert "pinned" in h2.refused()
    h3 = LiveHost(host.root / "c")
    h3.set_vault({"NTOPNG_IMAGE": "ntop/ntopng"})
    assert "pinned" in h3.refused()


def test_capabilities_are_listed_and_everything_else_dropped(host):
    host.ok()
    ex = host.exec_start()
    assert "--cap-drop ALL" in ex
    caps = re.findall(r"--cap-add (\w+)", ex)
    assert "NET_RAW" in caps and "NET_ADMIN" in caps
    h2 = LiveHost(host.root / "b", sknet_live_cap_add=["NET_RAW"])
    h2.ok()
    assert re.findall(r"--cap-add (\w+)", h2.exec_start()) == ["NET_RAW"]


def test_memory_is_always_set_on_container_and_unit(host):
    host.ok()
    ex, unit = host.exec_start(), host.unit_text()
    assert "--memory 512M --memory-swap 512M" in ex
    assert re.search(r"^MemoryMax=512M$", unit, re.M)
    h2 = LiveHost(host.root / "b", sknet_live_memory_max="768M")
    h2.ok()
    assert "--memory 768M --memory-swap 768M" in h2.exec_start() and "MemoryMax=768M" in h2.unit_text()
    # an empty value falls back to the default instead of rendering no limit
    h3 = LiveHost(host.root / "c", sknet_live_memory_max="")
    h3.ok()
    assert "MemoryMax=512M" in h3.unit_text() and "--memory 512M" in h3.exec_start()


def test_units_are_enabled_and_started_allowlist_first(host):
    host.ok()
    for u in ("sknet-live-fw.service", "sknet-live.service"):
        assert host.flag("enabled", u) and host.flag("active", u)
    starts = [c for c in host.calls() if c.startswith("start ")]
    assert starts.index("start sknet-live-fw.service") < starts.index("start sknet-live.service")
    assert (host.data / "ntopng").is_dir()
    assert "Requires=docker.service sknet-live-fw.service" in host.unit_text()


def test_missing_docker_fails_clearly(tmp_path):
    h = LiveHost(tmp_path, docker=False)
    # a PATH of its own, so a real docker on this machine cannot be found
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("sh", "bash", "env", "cat", "rm", "mkdir", "chmod", "ln", "cp", "mv", "stat", "sleep", "dirname", "basename", "touch", "true"):
        src = shutil.which(name)
        if src:
            (tools / name).symlink_to(src)
    (tools / "python3").symlink_to(sys.executable)
    ev = {**h.extra}
    cmd = [shutil.which("ansible-playbook"), "-i", str(h.inventory()), "-e", json.dumps(ev), str(PLAY)]
    env = h.env()
    env["PATH"] = f"{h.bin}:{tools}"
    proc = subprocess.run(cmd, cwd=PLAY.parent, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.returncode != 0 and "no docker" in proc.stdout, proc.stdout[-3000:]
    assert not (h.units / "sknet-live.service").exists()
    assert not (h.sbin / "sknet-live-fw").exists()


# --- guards -------------------------------------------------------------------

@pytest.mark.parametrize("bind,why", [
    ("0.0.0.0", "0.0.0.0"),
    ("live.example.test", "IPv4 literal"),
    ("", "IPv4 literal"),
    ("8.8.8.8", "public"),
    ("192.0.2.300", "IPv4 literal"),
])
def test_bad_bind_is_refused(tmp_path, bind, why):
    h = LiveHost(tmp_path, sknet_live_bind=bind)
    assert why in h.refused()
    assert not (h.units / "sknet-live.service").exists()


def test_public_bind_needs_the_explicit_flag(tmp_path):
    h = LiveHost(tmp_path, sknet_live_bind="8.8.8.8", sknet_live_allow_public_bind=True)
    h.ok()
    assert " -w 8.8.8.8:3000 " in h.exec_start()
    # the wildcard stays refused even then
    h2 = LiveHost(tmp_path / "b", sknet_live_bind="0.0.0.0", sknet_live_allow_public_bind=True)
    assert "0.0.0.0" in h2.refused()


@pytest.mark.parametrize("sources", [[], None, "192.0.2.5", ["live.example.test"], ["192.0.2.5/33"], ["0.0.0.0/0"], ["192.0.2.0/0"], ["10.0.0.0/4"]])
def test_bad_allow_sources_are_refused(tmp_path, sources):
    h = LiveHost(tmp_path)
    if sources is None:
        del h.vars["sknet_live_allow_sources"]
    else:
        h.vars["sknet_live_allow_sources"] = sources
    assert "sknet_live_allow_sources" in h.refused()
    assert not (h.sbin / "sknet-live-fw").exists()


@pytest.mark.parametrize("ifaces", [[], ["bad:name"], ["a" * 16], ["eth0", "eth0"], "eth0", ["eth 0"], [f"e{i}" for i in range(9)]])
def test_bad_interfaces_are_refused(tmp_path, ifaces):
    h = LiveHost(tmp_path, sknet_live_interfaces=ifaces)
    assert "sknet_live_interfaces" in h.refused()


@pytest.mark.parametrize("mem", ["lots", "0M", "512", "-1M", "512 M"])
def test_bad_memory_is_refused(tmp_path, mem):
    assert "sknet_live_memory_max" in LiveHost(tmp_path, sknet_live_memory_max=mem).refused()


def test_unknown_state_is_refused(tmp_path):
    assert "sknet_state" in LiveHost(tmp_path, sknet_state="paused").refused()


def test_missing_vault_is_refused_for_present_but_not_for_absent(tmp_path):
    h = LiveHost(tmp_path)
    h.set_vault(None)
    assert "no sknet vault" in h.refused()
    assert not (h.units / "sknet-live.service").exists()
    h2 = LiveHost(tmp_path / "b", sknet_state="absent")
    h2.set_vault(None)
    h2.ok()


def test_vault_is_loaded_by_the_play(host):
    # a vault value reaches the unit, so the play really read the file
    host.set_vault({"NTOPNG_IMAGE": "ntop/ntopng:7.0.2"})
    host.ok()
    assert "ntop/ntopng:7.0.2" in host.unit_text()


# --- the allowlist chain ------------------------------------------------------

def test_chain_accepts_only_the_listed_sources_then_drops(host):
    host.ok()
    host.apply_script()
    assert host.rules() == [
        "-A SKNET-LIVE -s 192.0.2.50 -j ACCEPT",
        "-A SKNET-LIVE -s 198.51.100.0/24 -j ACCEPT",
        "-A SKNET-LIVE -j DROP",
    ]
    assert host.hooks() == [HOOK]
    script = (host.sbin / "sknet-live-fw").read_text()
    assert "-j RETURN" not in script and "--privileged" not in script


def test_chain_script_is_idempotent_and_unit_runs_it(host):
    host.ok()
    host.apply_script()
    host.apply_script()
    assert host.hooks() == [HOOK]
    assert f"ExecStart={host.sbin}/sknet-live-fw" in (host.units / "sknet-live-fw.service").read_text()
    assert "Before=sknet-live.service" in (host.units / "sknet-live-fw.service").read_text()


def test_changing_the_sources_rewrites_the_script_and_reruns_it(host):
    host.ok()
    host.vars["sknet_live_allow_sources"] = ["192.0.2.99"]
    host.ok()
    assert "192.0.2.99" in (host.sbin / "sknet-live-fw").read_text()
    assert "192.0.2.50" not in (host.sbin / "sknet-live-fw").read_text()
    assert "restart sknet-live-fw.service" in host.calls()


# --- idempotency --------------------------------------------------------------

def test_second_run_changes_nothing(host):
    first = host.ok()
    assert first["changed"] and first["changed"] > 0
    calls_before = len(host.calls())
    second = host.ok()
    assert second["changed"] == 0, second["log"][-3000:]
    new = host.calls()[calls_before:]
    assert not any(c.split()[0] in ("start", "restart", "enable") for c in new)
    assert not any(c.startswith("docker pull") for c in new)


def test_changed_flags_restart_ntopng_only(host):
    host.ok()
    host.vars["sknet_live_interfaces"] = ["eth0"]
    host.ok()
    assert "restart sknet-live.service" in host.calls()
    assert "restart sknet-live-fw.service" not in host.calls()


# --- state absent -------------------------------------------------------------

def _present_then_absent(host, **absent_vars):
    host.ok()
    host.apply_script()
    (host.state / "container").touch()
    (host.data / "ntopng" / "keep.rrd").write_text("x")
    host.vars["sknet_state"] = "absent"
    return host.ok(**absent_vars)


def test_absent_removes_units_container_chain_and_hook_keeps_image_and_data(host):
    _present_then_absent(host)
    assert not (host.units / "sknet-live.service").exists()
    assert not (host.units / "sknet-live-fw.service").exists()
    assert not (host.sbin / "sknet-live-fw").exists()
    assert not host.flag("enabled", "sknet-live.service") and not host.flag("active", "sknet-live.service")
    assert not host.flag("enabled", "sknet-live-fw.service")
    assert not (host.state / "container").exists()
    assert host.hooks() == [] and not (host.state / "chain-SKNET-LIVE").exists()
    assert "docker rmi" not in " ".join(host.calls()) and "docker image rm" not in " ".join(host.calls())
    assert (host.state / "images").glob("*") and (host.data / "ntopng" / "keep.rrd").exists()


def test_absent_purges_data_only_when_asked(tmp_path):
    h = LiveHost(tmp_path)
    h.vars["sknet_live_purge_data"] = True
    _present_then_absent(h)
    assert not h.data.exists()


def test_absent_is_idempotent_and_ok_on_a_clean_host(host):
    _present_then_absent(host)
    again = host.ok()
    assert again["changed"] == 0, again["log"][-3000:]
    clean = LiveHost(host.root / "clean", sknet_state="absent")
    assert clean.ok()["changed"] == 0


def test_absent_does_not_need_docker_or_the_live_hostvars(tmp_path):
    h = LiveHost(tmp_path, docker=False, sknet_state="absent")
    h.vars.pop("sknet_live_bind")
    h.ok()


def test_short_prefix_refused_and_slash_8_accepted(tmp_path):
    LiveHost(tmp_path / "a", sknet_live_allow_sources=["10.0.0.0/8"]).ok()
    assert "sknet_live_allow_sources" in LiveHost(tmp_path / "b", sknet_live_allow_sources=["192.0.2.50", "10.0.0.0/7"]).refused()


def test_unit_hardening_and_log_cap(host):
    host.ok()
    ex = host.exec_start()
    for f in ("--security-opt no-new-privileges", "--log-opt max-size=10m", "--log-opt max-file=3"):
        assert f in ex
    fw = (host.units / "sknet-live-fw.service").read_text()
    assert "Wants=network-pre.target" in fw and "After=network-pre.target" in fw


# --- the review fix wave ------------------------------------------------------

def test_hook_matches_only_the_bind_address_and_replaces_a_legacy_hook(host):
    host.ok()
    # a hook from before the destination match is replaced, not left matching every address
    (host.state / "hooks").write_text("INPUT -p tcp --dport 3000 -j SKNET-LIVE\n")
    host.apply_script()
    assert host.hooks() == [HOOK]
    host.apply_script()
    assert host.hooks() == [HOOK]


def test_absent_removes_the_destination_scoped_hook_even_without_the_bind_hostvar(host):
    host.ok()
    host.apply_script()
    assert host.hooks() == [HOOK]
    host.vars["sknet_state"] = "absent"
    host.vars.pop("sknet_live_bind")
    host.ok()
    assert host.hooks() == [] and not (host.state / "chain-SKNET-LIVE").exists()


def test_a_live_host_that_is_a_swarm_node_gets_a_gwbridge_warning(host):
    (host.state / "swarm").touch()
    res = host.ok()
    assert "docker_gwbridge" in res["log"] and "swarm node" in res["log"]
    assert "sknet_live_allow_sources" in res["log"] and "PROXY_PLACEMENT_CONSTRAINTS" in res["log"]


def test_no_warning_on_a_host_outside_a_swarm(host):
    res = host.ok()
    assert "docker_gwbridge" not in res["log"]
