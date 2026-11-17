"""skpatch-hosts: sk-swarm-upgrade-gate makes manager docker upgrades serial.

The dashboard's update schedule upgrades its systems one after another but
only waits for apt to exit. A docker-ce/containerd.io upgrade restarts
dockerd, so three managers restarted it within about 90 seconds of each
other on a production estate (2026-11-17) and bounced the services they ran.
The APT hook holds each manager until the previous one's swarm has converged
plus a minimum gap, and stops every later manager if one never converges.

The script runs for real here, against a stub `docker` backed by a JSON state
file and a virtual clock (`date`/`sleep` stubs), so waits cost nothing.
"""
import json
import os
import pathlib
import subprocess

import pytest
import yaml

ROLE = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skpatch-hosts"
GATE = ROLE / "src/skpatch-hosts/sk-swarm-upgrade-gate.sh"
PROVISION = ROLE / "provision_skpatch_hosts_nodes.yml"

DOCKER_STUB = r'''#!/usr/bin/env python3
import json, os, sys
p = os.environ["GATE_STATE"]
st = json.load(open(p))
a = sys.argv[1:]
def save(): json.dump(st, open(p, "w"))
def nodes():
    # a scripted change of the swarm at a virtual time: [{"at": t, "nodes": [...], "services": [...]}]
    t = int(open(os.environ["GATE_CLOCK"]).read())
    cur = {"nodes": st["nodes"], "services": st["services"]}
    for step in st.get("timeline", []):
        if t >= step["at"]:
            cur.update({k: v for k, v in step.items() if k != "at"})
    return cur
st.setdefault("calls", []).append(" ".join(a)); save()
if a[:1] == ["info"]:
    if st.get("docker_down"):
        sys.exit(1)
    if len(a) == 1:
        sys.exit(0)
    fmt = a[2]
    if "ControlAvailable" in fmt: print("true" if st.get("manager", True) else "false")
    elif "LocalNodeState" in fmt: print("active" if st.get("member", True) else "inactive")
    elif "Swarm.Error" in fmt: print(st.get("swarm_error", ""))
    elif "NodeID" in fmt: print(st["self"])
    sys.exit(0)
if a[:1] == ["node"] and not st.get("manager", True):
    print("Error response from daemon: This node is not a swarm manager.", file=sys.stderr); sys.exit(1)
if a[:2] == ["ps", "-q"]:
    for c in st.get("containers", {}): print(c)
    sys.exit(0)
if a[:1] == ["inspect"]:
    print(" ".join(st.get("containers", {}).get(a[-1], [])) + " ")
    sys.exit(0)
if a[:2] == ["network", "ls"]:
    for n in st.get("overlays", {}): print(n)
    sys.exit(0)
if a[:2] == ["network", "inspect"]:
    print("true" if st.get("overlays", {}).get(a[-1]) else "false")
    sys.exit(0)
if a[:2] == ["node", "ls"]:
    cur = nodes()
    if "-q" in a:
        for n in cur["nodes"]: print(n["id"])
    else:
        for n in cur["nodes"]:
            print(" ".join([str(n["id"] == st["self"]).lower(), n["host"], n["status"], n["avail"], n.get("mstatus", "")]).rstrip())
    sys.exit(0)
if a[:2] == ["node", "inspect"]:
    key = a[3].split('"')[1]
    v = st.get("labels", {}).get(a[4], {}).get(key)
    print("<no value>" if v is None else v)
    sys.exit(0)
if a[:2] == ["node", "update"]:
    node = a[-1]
    lab = st.setdefault("labels", {}).setdefault(node, {})
    args = a[2:-1]
    new = dict(lab)
    for flag, val in zip(args[::2], args[1::2]):
        if flag == "--label-add":
            k, v = val.split("=", 1); new[k] = v
        elif flag == "--label-rm":
            if val not in new:  # like docker: refuse, change nothing
                print(f"key {val} doesn't exist in node's labels", file=sys.stderr); sys.exit(1)
            new.pop(val)
    lab.clear(); lab.update(new)
    save(); sys.exit(0)
if a[:2] == ["service", "ls"]:
    for s in nodes()["services"]: print(s)
    sys.exit(0)
sys.exit(99)
'''

DATE_STUB = r'''#!/bin/sh
t=$(cat "$GATE_CLOCK")
case "$*" in
  *%s*) echo "$t" ;;
  *) echo "20261117T000000Z" ;;
esac
'''

# ip and nsenter: vxlan devices per netns ("-" is the host netns) in st["links"].
IP_STUB = r'''#!/usr/bin/env python3
import json, os, sys
p = os.environ["GATE_STATE"]
st = json.load(open(p))
ns = os.environ.get("GATE_NS", "-")
a = sys.argv[1:]
st.setdefault("calls", []).append("ip " + ns + " " + " ".join(a))
links = st.setdefault("links", {}).setdefault(ns, [])
if a[:2] == ["-o", "link"] and "vxlan" in a:
    for i, d in enumerate(links): print(f"{i + 10}: {d}: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue")
elif a[:2] == ["link", "delete"]:
    if a[2] not in links: sys.exit(1)
    links.remove(a[2])
else:
    sys.exit(99)
json.dump(st, open(p, "w"))
'''

NSENTER_STUB = r'''#!/bin/sh
ns=${1#--net=}
shift
GATE_NS=$ns exec "$@"
'''

# journalctl -u docker --since @N: st["journal"] entries at or after N, plus one
# fresh rejection per call while a device in st["stale"] still exists (the swarm
# keeps placing tasks here, and each one fails until the device is gone).
JOURNAL_STUB = r'''#!/usr/bin/env python3
import json, os, sys
st = json.load(open(os.environ["GATE_STATE"]))
since = int(sys.argv[sys.argv.index("--since") + 1].lstrip("@"))
t = int(open(os.environ["GATE_CLOCK"]).read())
for e in st.get("journal", []):
    if e["at"] >= since: print(e["msg"])
present = {d for devs in st.get("links", {}).values() for d in devs}
if t >= since and present & set(st.get("stale", [])):
    print('fatal task error error="network sandbox join failed: network sandbox join failed for subnet '
          '\\"10.0.9.0/24\\": error creating vxlan interface: file exists"')
'''

SLEEP_STUB = r'''#!/bin/sh
t=$(cat "$GATE_CLOCK")
echo $((t + $1)) > "$GATE_CLOCK"
echo "$1" >> "$GATE_SLEEPS"
'''

MGRS = [
    {"id": "m1", "host": "mgr1", "status": "Ready", "avail": "Active", "mstatus": "Leader"},
    {"id": "m2", "host": "mgr2", "status": "Ready", "avail": "Active", "mstatus": "Reachable"},
    {"id": "m3", "host": "mgr3", "status": "Ready", "avail": "Active", "mstatus": "Reachable"},
    {"id": "w1", "host": "wkr1", "status": "Ready", "avail": "Active"},
]
OK_SERVICES = ["a_web replicated 1/1", "a_db replicated 1/1", "edge global 3/3 (max 1 per node)",
               "once replicated-job 0/1 (1/1 completed)"]
ENGINE = "/var/cache/apt/archives/docker-ce_5%3a29.10.0-1_amd64.deb\n"
OTHER = "/var/cache/apt/archives/libc6_2.39-0ubuntu8_amd64.deb\n"
T0 = 1_800_000_000


class Gate:
    def __init__(self, tmp, _state=None, **state):
        state = dict(_state or {}, **state)  # positional dict: fixtures may use the key "self"
        self.tmp = tmp
        bindir = tmp / "bin"
        bindir.mkdir()
        for name, body in (("docker", DOCKER_STUB), ("date", DATE_STUB), ("sleep", SLEEP_STUB),
                           ("ip", IP_STUB), ("nsenter", NSENTER_STUB), ("journalctl", JOURNAL_STUB),
                           ("logger", "#!/bin/sh\nexit 0\n")):
            f = bindir / name
            f.write_text(body)
            f.chmod(0o755)
        self.clock = tmp / "clock"
        self.clock.write_text(str(T0))
        self.sleeps = tmp / "sleeps"
        self.sleeps.write_text("")
        self.state = tmp / "state.json"
        base = {"self": "m2", "nodes": MGRS, "services": OK_SERVICES, "labels": {}}
        base.update(state)
        # "netns:<name>" in links stands for docker's netns file <NETNS_DIR>/<name>
        self.netns = tmp / "netns"
        self.netns.mkdir()
        links = {}
        for ns, devs in base.get("links", {}).items():
            if ns.startswith("netns:"):
                f = self.netns / ns.split(":", 1)[1]
                f.write_text("")
                ns = str(f)
            links[ns] = devs
        base["links"] = links
        self.state.write_text(json.dumps(base))
        self.shared = tmp / "shared"
        conf = tmp / "gate.conf"
        conf.write_text("GAP_S=600\nPRE_TIMEOUT_S=900\nPOST_TIMEOUT_S=900\nMIN_SETTLE_S=60\nPOLL_S=15\n")
        self.env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", GATE_STATE=str(self.state),
                        GATE_CLOCK=str(self.clock), GATE_SLEEPS=str(self.sleeps),
                        SK_SWARM_GATE_CONF=str(conf), STATE_DIR=str(tmp / "run"),
                        SHARED_DIR=str(tmp / "shared"), NETNS_DIR=str(tmp / "netns"))

    def run(self, mode, stdin="", *extra, **env):
        e = dict(self.env, **env)
        return subprocess.run(["sh", str(GATE), mode, *extra], input=stdin, env=e,
                              capture_output=True, text=True)

    @property
    def st(self):
        return json.loads(self.state.read_text())

    def slept(self):
        return sum(int(x) for x in self.sleeps.read_text().split())

    def now(self):
        return int(self.clock.read_text())


def test_non_engine_upgrade_passes_untouched(tmp_path):
    g = Gate(tmp_path)
    r = g.run("pre", OTHER)
    assert r.returncode == 0
    assert g.st.get("calls", []) == []  # never even asked docker
    assert not (tmp_path / "run/pending").exists()
    assert g.run("post").returncode == 0 and g.slept() == 0


def test_non_swarm_host_passes(tmp_path):
    g = Gate(tmp_path, manager=False, member=False)
    assert g.run("pre", ENGINE).returncode == 0
    assert not (tmp_path / "run/pending").exists()


def test_healthy_swarm_lets_engine_through_then_post_labels_done(tmp_path):
    g = Gate(tmp_path)
    assert g.run("pre", ENGINE).returncode == 0, g
    assert (tmp_path / "run/pending").exists()
    r = g.run("post")
    assert r.returncode == 0, r.stderr
    assert g.st["labels"]["m2"]["sk.skpatch.docker-upgraded-at"] == str(g.now())
    assert g.slept() >= 60  # MIN_SETTLE_S, then two healthy polls
    assert not (tmp_path / "run/pending").exists()


def test_held_swarm_refuses(tmp_path):
    g = Gate(tmp_path, labels={"m1": {"sk.skpatch.hold": "20261117T025200Z-mgr1"}})
    r = g.run("pre", ENGINE)
    assert r.returncode == 1
    assert "REFUSED" in r.stderr and "held" in r.stderr
    assert not (tmp_path / "run/pending").exists()


def test_gap_since_previous_manager_is_waited_out(tmp_path):
    g = Gate(tmp_path, labels={"m1": {"sk.skpatch.docker-upgraded-at": str(T0 - 100)}})
    r = g.run("pre", ENGINE)
    assert r.returncode == 0, r.stderr
    assert g.slept() == 500  # 600 s gap, 100 already passed


def test_gap_already_passed_does_not_wait(tmp_path):
    g = Gate(tmp_path, labels={"m1": {"sk.skpatch.docker-upgraded-at": str(T0 - 4000)}})
    assert g.run("pre", ENGINE).returncode == 0
    assert g.slept() == 0


def test_unreachable_manager_refuses_after_bounded_wait(tmp_path):
    bad = [dict(n) for n in MGRS]
    bad[2]["mstatus"] = "Unreachable"
    bad[2]["status"] = "Down"
    g = Gate(tmp_path, nodes=bad)
    r = g.run("pre", ENGINE)
    assert r.returncode == 1
    assert "mgr3" in r.stderr
    assert 900 <= g.slept() <= 900 + 15
    assert not (tmp_path / "run/pending").exists()


def test_manager_that_recovers_within_timeout_is_waited_for(tmp_path):
    bad = [dict(n) for n in MGRS]
    bad[0]["mstatus"] = "Unreachable"
    g = Gate(tmp_path, nodes=bad, timeline=[{"at": T0 + 120, "nodes": MGRS}])
    assert g.run("pre", ENGINE).returncode == 0
    assert 120 <= g.slept() < 900


def test_post_waits_for_services_to_come_back(tmp_path):
    down = ["a_web replicated 0/1", "a_db replicated 1/1", "edge global 2/3"]
    g = Gate(tmp_path, timeline=[{"at": T0 + 1, "services": down}, {"at": T0 + 300, "services": OK_SERVICES}])
    assert g.run("pre", ENGINE).returncode == 0  # at T0, healthy
    r = g.run("post")
    assert r.returncode == 0, r.stderr
    assert g.now() >= T0 + 300 + 15  # waited for the services, then one more healthy poll
    assert "sk.skpatch.docker-upgraded-at" in g.st["labels"]["m2"]


def test_post_timeout_sets_hold_and_fails(tmp_path):
    g = Gate(tmp_path)
    assert g.run("pre", ENGINE).returncode == 0
    st = g.st
    st["services"] = ["a_web replicated 0/1", "a_db replicated 1/1"]
    g.state.write_text(json.dumps(st))
    r = g.run("post")
    assert r.returncode == 1
    assert "FAILED" in r.stderr and "a_web" in r.stderr
    assert g.st["labels"]["m2"]["sk.skpatch.hold"].endswith(os.uname().nodename)
    assert "sk.skpatch.docker-upgraded-at" not in g.st["labels"].get("m2", {})


def test_after_a_failed_manager_no_later_manager_upgrades(tmp_path):
    g = Gate(tmp_path)
    assert g.run("pre", ENGINE).returncode == 0
    st = g.st
    st["services"] = ["a_web replicated 0/1"]
    g.state.write_text(json.dumps(st))
    assert g.run("post").returncode == 1
    # the swarm recovers later, but the hold stops the next manager anyway
    st = g.st
    st["services"] = OK_SERVICES
    st["self"] = "m3"
    g.state.write_text(json.dumps(st))
    r = g.run("pre", ENGINE)
    assert r.returncode == 1 and "held" in r.stderr


def test_preexisting_short_service_is_not_blamed(tmp_path):
    broken = OK_SERVICES + ["legacy replicated 0/1"]
    g = Gate(tmp_path, services=broken)
    r = g.run("pre", ENGINE)
    assert r.returncode == 0
    assert "legacy" in r.stderr
    assert g.run("post").returncode == 0


def test_dockerd_down_counts_as_not_converged(tmp_path):
    g = Gate(tmp_path)
    assert g.run("pre", ENGINE).returncode == 0
    st = g.st
    st["docker_down"] = True
    g.state.write_text(json.dumps(st))
    r = g.run("post")
    assert r.returncode == 1 and "dockerd not answering" in r.stderr


def test_three_managers_in_one_window_are_spaced_and_converged(tmp_path):
    """The real schedule shape: mgr1, mgr2, mgr3 back to back in one chain."""
    g = Gate(tmp_path)
    for node in ("m1", "m2", "m3"):
        st = g.st
        st["self"] = node
        g.state.write_text(json.dumps(st))
        assert g.run("pre", ENGINE).returncode == 0
        assert g.run("post").returncode == 0
    labels = g.st["labels"]
    done = [int(labels[n]["sk.skpatch.docker-upgraded-at"]) for n in ("m1", "m2", "m3")]
    assert done[1] - done[0] >= 600 + 60 and done[2] - done[1] >= 600 + 60


def test_dry_run_has_no_side_effects(tmp_path):
    g = Gate(tmp_path, labels={"m1": {"sk.skpatch.docker-upgraded-at": str(T0 - 100)}})
    r = g.run("pre", ENGINE, "--dry-run")
    assert r.returncode == 0
    assert "waiting 500s" in r.stderr and "would let" in r.stderr
    assert g.slept() == 0
    assert not (tmp_path / "run").exists()
    r = g.run("post", "", "--dry-run")
    assert r.returncode == 0 and "converged now" in r.stderr
    assert not any(c.startswith("node update") for c in g.st["calls"])


def test_skip_env_bypasses(tmp_path):
    g = Gate(tmp_path, labels={"m1": {"sk.skpatch.hold": "x"}})
    assert g.run("pre", ENGINE, SK_SWARM_GATE_SKIP="1").returncode == 0


def test_containerd_counts_as_engine(tmp_path):
    g = Gate(tmp_path, labels={"m1": {"sk.skpatch.hold": "x"}})
    assert g.run("pre", "/var/cache/apt/archives/containerd.io_2.2.1-1_amd64.deb\n").returncode == 1
    assert g.run("pre", "/var/cache/apt/archives/docker-ce-cli_5%3a29.10.0-1_amd64.deb\n").returncode == 0


def test_gate_is_posix_sh():
    if not pathlib.Path("/bin/dash").exists():
        pytest.skip("dash not installed")
    assert subprocess.run(["dash", "-n", str(GATE)]).returncode == 0


# --- the provisioning playbook wires it on every swarm node -----------------

def _tasks():
    plays = yaml.safe_load(PROVISION.read_text())
    return [t for t in plays[0]["tasks"]]


def test_provision_installs_gate_and_apt_hook():
    tasks = {t["name"]: t for t in _tasks()}
    names = " | ".join(tasks)
    assert "swarm upgrade gate" in names
    hook = next(t for n, t in tasks.items() if "APT hook" in n and "absent" not in n.lower())
    content = hook["copy"]["content"]
    assert 'DPkg::Pre-Install-Pkgs { "/usr/local/sbin/sk-swarm-upgrade-gate pre"; };' in content
    assert 'DPkg::Post-Invoke { "/usr/local/sbin/sk-swarm-upgrade-gate post"; };' in content
    assert "skp_swarm_gate" in str(hook["when"])


def test_provision_gate_on_every_swarm_member_and_removable():
    text = PROVISION.read_text()
    assert "Swarm.LocalNodeState" in text and "== 'active'" in text  # workers too, not only managers
    assert "Swarm.ControlAvailable" not in text
    assert "skpatch_hosts_swarm_gate" in text
    removal = [t for t in _tasks() if "gate" in t["name"].lower() and "remove" in t["name"].lower()]
    assert removal, "no removal path for the gate"


def test_busy_label_set_while_upgrading_and_cleared_after(tmp_path):
    g = Gate(tmp_path)
    assert g.run("pre", ENGINE).returncode == 0
    assert g.st["labels"]["m2"]["sk.skpatch.docker-upgrading"] == str(T0)
    assert g.run("post").returncode == 0
    assert "sk.skpatch.docker-upgrading" not in g.st["labels"]["m2"]


def test_other_manager_mid_upgrade_is_waited_for_then_refused(tmp_path):
    g = Gate(tmp_path, labels={"m1": {"sk.skpatch.docker-upgrading": str(T0 - 30)}})
    r = g.run("pre", ENGINE)
    assert r.returncode == 1 and "still upgrading" in r.stderr
    assert 900 <= g.slept() <= 915
    assert "sk.skpatch.docker-upgrading" not in g.st["labels"].get("m2", {})


def test_stale_busy_label_is_ignored(tmp_path):
    g = Gate(tmp_path, labels={"m1": {"sk.skpatch.docker-upgrading": str(T0 - 4000)}})
    assert g.run("pre", ENGINE).returncode == 0
    assert g.slept() == 0


def test_post_timeout_clears_busy_and_sets_hold(tmp_path):
    g = Gate(tmp_path)
    assert g.run("pre", ENGINE).returncode == 0
    st = g.st
    st["services"] = ["a_web replicated 0/1"]
    g.state.write_text(json.dumps(st))
    assert g.run("post").returncode == 1
    lab = g.st["labels"]["m2"]
    assert "sk.skpatch.hold" in lab and "sk.skpatch.docker-upgrading" not in lab


def test_done_label_set_even_when_busy_label_is_already_gone(tmp_path):
    """docker refuses --label-rm of a missing key; that must not lose the done label."""
    g = Gate(tmp_path)
    assert g.run("pre", ENGINE).returncode == 0
    st = g.st
    st["labels"]["m2"].pop("sk.skpatch.docker-upgrading")
    g.state.write_text(json.dumps(st))
    assert g.run("post").returncode == 0
    assert "sk.skpatch.docker-upgraded-at" in g.st["labels"]["m2"]


# --- workers, the shared hold and the stale vxlan repair -------------------
# 2026-11-17, a production worker: after its docker-ce upgrade a stale overlay
# vxlan device made every task placed there fail with "error creating vxlan
# interface: file exists" for 11 hours, and nothing noticed.

NET_STALE = "abcde0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
NET_USED = "f00ba0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
NET_INGRESS = "1ngre0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
DEV_STALE = "vx-001001-abcde"
DEV_USED = "vx-001002-f00ba"
DEV_INGRESS = "vx-001000-1ngre"
WORKER = {"self": "w1", "manager": False,
          "containers": {"c1": [NET_USED]},
          "overlays": {NET_USED: False, NET_INGRESS: True}}


def worker(tmp_path, **state):
    return Gate(tmp_path, dict(WORKER, **state))


def _ip_deletes(g):
    return [c for c in g.st["calls"] if c.startswith("ip ") and " link delete " in c]


def test_worker_healthy_upgrade_passes_without_manager_calls(tmp_path):
    g = worker(tmp_path, links={"netns:1-f00ba01234": [DEV_USED]})
    assert g.run("pre", ENGINE).returncode == 0
    assert (tmp_path / "run/pending").read_text().split() == [str(T0), "worker"]
    r = g.run("post")
    assert r.returncode == 0, r.stderr
    assert "worker back in the swarm" in r.stderr
    calls = g.st["calls"]
    assert not any(c.startswith(("node ", "service ")) for c in calls)  # a worker cannot ask
    assert _ip_deletes(g) == [] and not (tmp_path / "shared/hold").exists()


def test_worker_stale_vxlan_is_removed_and_only_that(tmp_path):
    """The stale device goes (host netns); the overlay in use and the ingress stay."""
    g = worker(tmp_path, stale=[DEV_STALE],
               links={"-": [DEV_STALE], "netns:1-f00ba01234": [DEV_USED], "netns:ingress_sbox": [DEV_INGRESS]})
    assert g.run("pre", ENGINE).returncode == 0
    r = g.run("post")
    assert r.returncode == 0, r.stderr
    assert "rejected" in r.stderr and f"deleted stale {DEV_STALE}" in r.stderr
    links = g.st["links"]
    assert links["-"] == []
    assert links[str(tmp_path / "netns/1-f00ba01234")] == [DEV_USED]
    assert links[str(tmp_path / "netns/ingress_sbox")] == [DEV_INGRESS]
    assert len(_ip_deletes(g)) == 1
    assert not (tmp_path / "shared/hold").exists()


def test_stale_vxlan_inside_a_docker_netns_is_removed_there(tmp_path):
    g = worker(tmp_path, stale=[DEV_STALE], links={"netns:1-abcde01234": [DEV_STALE]})
    assert g.run("pre", ENGINE).returncode == 0
    r = g.run("post")
    assert r.returncode == 0, r.stderr
    ns = str(tmp_path / "netns/1-abcde01234")
    assert g.st["links"][ns] == []
    assert _ip_deletes(g) == [f"ip {ns} link delete {DEV_STALE}"]


def test_rejections_before_the_upgrade_are_not_counted(tmp_path):
    old = {"at": T0 - 600, "msg": "fatal task error ... error creating vxlan interface: file exists"}
    g = worker(tmp_path, journal=[old], links={"-": [DEV_STALE]})
    assert g.run("pre", ENGINE).returncode == 0
    assert g.run("post").returncode == 0
    assert _ip_deletes(g) == []  # nothing rejected since pre, nothing touched


def test_unrepairable_vxlan_sets_the_shared_hold_and_fails(tmp_path):
    """The stale device is the ingress's: never deleted, so it never converges."""
    g = worker(tmp_path, stale=[DEV_INGRESS], links={"netns:ingress_sbox": [DEV_INGRESS]})
    assert g.run("pre", ENGINE).returncode == 0
    r = g.run("post")
    assert r.returncode == 1
    assert "FAILED" in r.stderr and "vxlan" in r.stderr
    assert _ip_deletes(g) == []
    hold = (tmp_path / "shared/hold").read_text()
    assert hold.split()[0].endswith(os.uname().nodename) and "vxlan" in hold
    assert 900 <= g.slept() - 60 <= 915


def test_failed_worker_stops_the_next_worker_and_every_manager(tmp_path):
    g = worker(tmp_path, stale=[DEV_INGRESS], links={"netns:ingress_sbox": [DEV_INGRESS]})
    assert g.run("pre", ENGINE).returncode == 0
    assert g.run("post").returncode == 1
    st = g.st
    st["stale"] = []  # the swarm is fine again, the hold still stands
    g.state.write_text(json.dumps(st))
    r = g.run("pre", ENGINE)
    assert r.returncode == 1 and "held" in r.stderr
    st = g.st
    st.update({"self": "m1", "manager": True})
    g.state.write_text(json.dumps(st))
    r = g.run("pre", ENGINE)
    assert r.returncode == 1 and "held" in r.stderr and "shared" in r.stderr


def test_failed_manager_writes_the_shared_hold_for_workers(tmp_path):
    g = Gate(tmp_path)
    assert g.run("pre", ENGINE).returncode == 0
    st = g.st
    st["services"] = ["a_web replicated 0/1"]
    g.state.write_text(json.dumps(st))
    assert g.run("post").returncode == 1
    assert (tmp_path / "shared/hold").exists() and "sk.skpatch.hold" in g.st["labels"]["m2"]
    st = g.st
    st.update(WORKER)
    st["services"] = OK_SERVICES
    g.state.write_text(json.dumps(st))
    assert g.run("pre", ENGINE).returncode == 1


def test_manager_post_also_repairs_stale_vxlan(tmp_path):
    g = Gate(tmp_path, stale=[DEV_STALE], links={"-": [DEV_STALE]}, containers={}, overlays={})
    assert g.run("pre", ENGINE).returncode == 0
    r = g.run("post")
    assert r.returncode == 0, r.stderr
    assert g.st["links"]["-"] == [] and "sk.skpatch.docker-upgraded-at" in g.st["labels"]["m2"]


def test_worker_that_leaves_the_swarm_fails(tmp_path):
    g = worker(tmp_path)
    assert g.run("pre", ENGINE).returncode == 0
    st = g.st
    st.update(member=False, swarm_error="rpc error: code = Unavailable")
    g.state.write_text(json.dumps(st))
    r = g.run("post")
    assert r.returncode == 1 and "swarm state is inactive" in r.stderr and "rpc error" in r.stderr
    assert (tmp_path / "shared/hold").exists()


def test_clear_hold_on_a_manager_clears_file_and_labels(tmp_path):
    g = Gate(tmp_path, labels={"m1": {"sk.skpatch.hold": "x"}, "m3": {"sk.skpatch.hold": "y"}})
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared/hold").write_text("20261117T103100Z-wkr1 vxlan\n")
    assert g.run("pre", ENGINE).returncode == 1
    r = g.run("clear-hold")
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "shared/hold").exists()
    assert all("sk.skpatch.hold" not in v for v in g.st["labels"].values())
    assert g.run("pre", ENGINE).returncode == 0


def test_worker_dry_run_reports_but_deletes_nothing(tmp_path):
    g = worker(tmp_path, stale=[DEV_STALE], links={"-": [DEV_STALE]})
    r = g.run("post", "", "--dry-run")
    assert r.returncode == 1  # rejections in the last hour: not converged
    assert f"would delete stale {DEV_STALE}" in r.stderr
    assert g.st["links"]["-"] == [DEV_STALE] and _ip_deletes(g) == []
    assert not (tmp_path / "shared").exists() and not (tmp_path / "run").exists()
    r = g.run("pre", ENGINE, "--dry-run")
    assert r.returncode == 0 and "worker" in r.stderr


def test_without_shared_storage_a_manager_still_holds_by_label(tmp_path):
    g = Gate(tmp_path)
    blocker = tmp_path / "shared"
    blocker.write_text("")  # a file where the directory should be: unwritable
    assert g.run("pre", ENGINE).returncode == 0
    st = g.st
    st["services"] = ["a_web replicated 0/1"]
    g.state.write_text(json.dumps(st))
    r = g.run("post")
    assert r.returncode == 1 and "could not write" in r.stderr
    assert "sk.skpatch.hold" in g.st["labels"]["m2"]


def test_pending_file_from_the_previous_version_means_manager(tmp_path):
    g = Gate(tmp_path)
    (tmp_path / "run").mkdir()
    (tmp_path / "run/pending").write_text("")
    assert g.run("post").returncode == 0
    assert "sk.skpatch.docker-upgraded-at" in g.st["labels"]["m2"]


def test_provision_writes_the_shared_hold_dir():
    settings = next(t for t in _tasks() if t["name"].startswith("Write the swarm upgrade gate settings"))
    content = settings["copy"]["content"]
    assert "SHARED_DIR=" in content and "/var/data/skpatch-swarm-gate" in content
    assert "SWARM_GATE_SHARED_DIR" in content
