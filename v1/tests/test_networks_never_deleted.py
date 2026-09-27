"""A deploy must never delete an existing overlay network.

The v1 network-setup logic (the per-service deploy.j2 scripts and the shared
create_networks.yml task) used to `docker network rm` + recreate any existing
network whose live subnet differed from the configured one (or, in
create_networks.yml, whose subnet it failed to parse, which was every one).
Shared networks such as `cloud-public-<env>` are attached to other stacks'
services, so deploying service A could delete the network service B runs on,
and deleting a service's own network cuts it off mid-deploy.

Rules now:
  * exists with the configured subnet  -> reused (no rm, no create)
  * exists with a different subnet     -> the deploy stops with an error that
    names the network, the live and configured subnets and the vault key to
    set to adopt the live subnet; nothing is removed
  * missing                            -> created
  * NETWORK_RECREATE_ON_MISMATCH=true  -> only the service's OWN network
    (<app>-<env>) may be recreated; shared cloud-* networks never are.

Behavioural: every deploy.j2 with subnet-check logic is rendered and run, and
the real create_networks.yml runs under ansible-playbook, both against a fake
`docker` that keeps network state in a file and logs every call.
"""
import os
import pathlib
import re
import signal
import stat
import subprocess

import jinja2
import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
V1 = REPO / "v1"
CREATE_NETWORKS = V1 / "ansible/shared/tasks/create_networks.yml"

# Every deploy.j2 that checks the live subnet of an existing network.
SUBNET_CHECK_DEPLOYS = sorted(
    p for p in (V1 / "ansible").glob("*/*/src/*/deploy.j2")
    if "IPAM" in p.read_text()
)
SERVICES = [p.parent.name for p in SUBNET_CHECK_DEPLOYS]

LIVE_OTHER = "10.99.0.0/24"
CONFIGURED = {"own": "172.16.77.0/24", "cloud-public-dev": "172.16.202.0/24"}

FAKE_DOCKER = r"""#!/bin/bash
echo "$*" >> "$DOCKERLOG"
lookup() { grep "^$1 " "$NETSTATE" | head -1 | cut -d' ' -f2; }
case "$1 $2" in
  "network ls") cut -d' ' -f1 "$NETSTATE" ;;
  "network inspect")
    grep -q "^$3 " "$NETSTATE" || { echo "Error: No such network: $3" >&2; exit 1; }
    s=$(lookup "$3")
    case "$*" in
      *"len .Containers"*) echo 0 ;;
      *Containers*) echo "" ;;
      *"range .IPAM.Config"*) echo "$s" ;;
      *IPAM.Config*) echo "[{$s  map[]}]" ;;
      *) echo "[]" ;;
    esac ;;
  "network create")
    name="${@: -1}"; subnet=""; prev=""
    for a in "$@"; do [ "$prev" = "--subnet" ] && subnet="$a"; prev="$a"; done
    grep -q "^$name " "$NETSTATE" && { echo "Error: network with name $name already exists" >&2; exit 1; }
    echo "$name $subnet" >> "$NETSTATE" ;;
  "network rm")
    grep -v "^$3 " "$NETSTATE" > "$NETSTATE.tmp"; mv "$NETSTATE.tmp" "$NETSTATE" ;;
  "stack deploy")
    touch "$REACHED"; kill -TERM "$PPID"; exit 0 ;;
  "service inspect") exit 1 ;;
  *) : ;;
esac
"""


def _bin(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("sleep", "#!/bin/sh\nexit 0\n")):
        f = bindir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return bindir


def _vault_key(svc, env="dev"):
    """The vault variable the service's playbook feeds app_networks from."""
    pb = (V1 / f"ansible/optional/{svc}/deploy_{svc}-{env}.yml").read_text()
    m = re.search(r'app_networks:\s*"\{\{\s*([\w.]+)\s*\|\s*default', pb)
    assert m, f"{svc}: no app_networks line in its {env} playbook"
    return m.group(1)


def _render(svc, tmp_path):
    src = V1 / f"ansible/optional/{svc}/src/{svc}/deploy.j2"
    app_networks = [
        {"name": f"{svc}-dev", "subnet": CONFIGURED["own"]},
        {"name": "cloud-public-dev", "subnet": CONFIGURED["cloud-public-dev"]},
    ]
    tmpl = jinja2.Environment(undefined=jinja2.ChainableUndefined).from_string(src.read_text())
    script = tmpl.render(env="dev", app=svc, app_networks=app_networks, **{svc: {}})
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in set(re.findall(r'COMPOSE_FILE="\$\{\w*CONFIG_DIR\}/([\w.-]+)"', script)) | {"docker-compose.yml"}:
        (config_dir / name).write_text("services: {}\n")
    assert "/var/data/config/${STACK_NAME}" in script, svc
    script = script.replace("/var/data/config/${STACK_NAME}", str(config_dir))
    networks = dict(re.findall(r'^\s*(?:NETWORKS)?\["([\w.-]+)"\]="([\d./]+)"\s*$', script, re.M))
    assert f"{svc}-dev" in networks and "cloud-public-dev" in networks, (svc, networks)
    deploy = tmp_path / "deploy.sh"
    deploy.write_text(script)
    return deploy, networks


def _run_deploy(svc, tmp_path, live, knob=False):
    """Run svc's rendered deploy.j2 with `live` = {network: subnet} pre-existing."""
    deploy, networks = _render(svc, tmp_path)
    state = tmp_path / "netstate"
    state.write_text("".join(f"{n} {s}\n" for n, s in live(networks).items()))
    log = tmp_path / "dockerlog"
    log.write_text("")
    reached = tmp_path / "reached"
    env = dict(
        os.environ,
        PATH=f"{_bin(tmp_path)}:{os.environ['PATH']}",
        DOCKERLOG=str(log), NETSTATE=str(state), REACHED=str(reached),
    )
    env.pop("NETWORK_RECREATE_ON_MISMATCH", None)
    if knob:
        env["NETWORK_RECREATE_ON_MISMATCH"] = "true"
    proc = subprocess.Popen(
        ["bash", str(deploy)], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        pytest.fail(f"{svc}: deploy hung:\n{out}")
    calls = log.read_text().splitlines()
    return dict(
        rc=proc.returncode, out=out, calls=calls, networks=networks,
        reached=reached.exists(),
        state=dict(l.split(" ", 1) for l in state.read_text().splitlines() if l),
    )


def _rm(calls):
    return [c for c in calls if c.startswith("network rm")]


def _create(calls):
    return [c for c in calls if c.startswith("network create")]


def test_discovered_subnet_check_deploys():
    # Sanity floor so a scan change cannot silently match nothing.
    assert len(SUBNET_CHECK_DEPLOYS) >= 9, SERVICES


@pytest.mark.parametrize("svc", SERVICES)
def test_matching_subnet_is_reused(svc, tmp_path):
    r = _run_deploy(svc, tmp_path, live=lambda n: dict(n))
    assert r["reached"], f"{svc}: stack deploy never ran:\n{r['out']}"
    assert not _rm(r["calls"]) and not _create(r["calls"]), (svc, r["calls"])


@pytest.mark.parametrize("svc", SERVICES)
def test_missing_network_is_created(svc, tmp_path):
    r = _run_deploy(svc, tmp_path, live=lambda n: {})
    assert r["reached"], f"{svc}: stack deploy never ran:\n{r['out']}"
    assert not _rm(r["calls"]), r["calls"]
    assert r["state"] == r["networks"], (svc, r["state"])


@pytest.mark.parametrize("svc", SERVICES)
def test_mismatched_own_network_stops_without_deleting(svc, tmp_path):
    own = f"{svc}-dev"
    r = _run_deploy(svc, tmp_path, live=lambda n: {**n, own: LIVE_OTHER})
    assert not _rm(r["calls"]), f"{svc}: deploy deleted a live network: {_rm(r['calls'])}"
    assert r["rc"] != 0 and not r["reached"], f"{svc}: deploy continued past a subnet mismatch:\n{r['out']}"
    assert r["state"][own] == LIVE_OTHER
    for needle in (own, LIVE_OTHER, r["networks"][own], _vault_key(svc)):
        assert needle in r["out"], f"{svc}: error does not mention {needle!r}:\n{r['out']}"


@pytest.mark.parametrize("svc", SERVICES)
def test_mismatched_shared_network_stops_without_deleting(svc, tmp_path):
    r = _run_deploy(svc, tmp_path, live=lambda n: {**n, "cloud-public-dev": LIVE_OTHER})
    assert not _rm(r["calls"]), f"{svc}: deploy deleted a shared network: {_rm(r['calls'])}"
    assert r["rc"] != 0 and not r["reached"], r["out"]
    assert "cloud-public-dev" in r["out"] and LIVE_OTHER in r["out"]


@pytest.mark.parametrize("svc", SERVICES)
def test_knob_recreates_own_network(svc, tmp_path):
    own = f"{svc}-dev"
    r = _run_deploy(svc, tmp_path, live=lambda n: {**n, own: LIVE_OTHER}, knob=True)
    assert r["reached"], f"{svc}: stack deploy never ran:\n{r['out']}"
    assert _rm(r["calls"]) == [f"network rm {own}"], r["calls"]
    assert r["state"][own] == r["networks"][own]


@pytest.mark.parametrize("svc", SERVICES)
def test_knob_never_recreates_shared_network(svc, tmp_path):
    r = _run_deploy(svc, tmp_path, live=lambda n: {**n, "cloud-public-dev": LIVE_OTHER}, knob=True)
    assert not _rm(r["calls"]), f"{svc}: knob deleted a shared cloud-* network: {_rm(r['calls'])}"
    assert r["rc"] != 0 and not r["reached"], r["out"]
    assert r["state"]["cloud-public-dev"] == LIVE_OTHER


@pytest.mark.parametrize("svc", SERVICES)
def test_deploy_honors_app_networks(svc, tmp_path):
    """The vault key named in the error must actually drive the subnet."""
    _, networks = _render(svc, tmp_path)
    assert networks == {f"{svc}-dev": CONFIGURED["own"], "cloud-public-dev": CONFIGURED["cloud-public-dev"]}


# --- shared/tasks/create_networks.yml, run by the real ansible-playbook -------

PLAY = """
- hosts: localhost
  gather_facts: false
  vars:
    app: zzsvc
    env: dev
    app_networks:
      - {{name: zzsvc-dev, subnet: "172.16.77.0/24"}}
      - {{name: cloud-public-dev, subnet: "172.16.202.0/24"}}
  environment:
    PATH: "{bindir}:{{{{ lookup('env', 'PATH') }}}}"
    DOCKERLOG: "{log}"
    NETSTATE: "{state}"
    REACHED: "{reached}"
  tasks:
    - import_tasks: {tasks}
"""


def _run_play(tmp_path, live, knob=False):
    state = tmp_path / "netstate"
    state.write_text("".join(f"{n} {s}\n" for n, s in live.items()))
    log = tmp_path / "dockerlog"
    log.write_text("")
    play = tmp_path / "play.yml"
    play.write_text(PLAY.format(
        bindir=_bin(tmp_path), log=log, state=state, reached=tmp_path / "reached",
        tasks=CREATE_NETWORKS,
    ))
    inv = tmp_path / "inv.ini"
    inv.write_text("localhost ansible_connection=local ansible_python_interpreter=auto_silent\n")
    extra = ["-e", "network_recreate_on_mismatch=true"] if knob else []
    proc = subprocess.run(
        ["ansible-playbook", "-i", str(inv), str(play), *extra],
        capture_output=True, text=True, timeout=180,
    )
    return dict(
        rc=proc.returncode, out=proc.stdout + proc.stderr,
        calls=log.read_text().splitlines(),
        state=dict(l.split(" ", 1) for l in state.read_text().splitlines() if l),
    )


PLAY_NETS = {"zzsvc-dev": "172.16.77.0/24", "cloud-public-dev": "172.16.202.0/24"}


def test_task_matching_subnet_is_reused(tmp_path):
    r = _run_play(tmp_path, dict(PLAY_NETS))
    assert r["rc"] == 0, r["out"]
    assert not _rm(r["calls"]) and not _create(r["calls"]), r["calls"]


def test_task_missing_network_is_created(tmp_path):
    r = _run_play(tmp_path, {})
    assert r["rc"] == 0, r["out"]
    assert not _rm(r["calls"])
    assert r["state"] == PLAY_NETS


def test_task_mismatch_fails_without_deleting(tmp_path):
    r = _run_play(tmp_path, {**PLAY_NETS, "zzsvc-dev": LIVE_OTHER})
    assert not _rm(r["calls"]), f"create_networks.yml deleted a live network: {_rm(r['calls'])}"
    assert r["rc"] != 0, r["out"]
    for needle in ("zzsvc-dev", LIVE_OTHER, "172.16.77.0/24", "zzsvc.networks", "zzsvc_dev_networks"):
        assert needle in r["out"], f"error does not mention {needle!r}:\n{r['out']}"


def test_task_shared_mismatch_fails_without_deleting(tmp_path):
    r = _run_play(tmp_path, {**PLAY_NETS, "cloud-public-dev": LIVE_OTHER})
    assert not _rm(r["calls"]), _rm(r["calls"])
    assert r["rc"] != 0, r["out"]


def test_task_knob_recreates_own_network(tmp_path):
    r = _run_play(tmp_path, {**PLAY_NETS, "zzsvc-dev": LIVE_OTHER}, knob=True)
    assert r["rc"] == 0, r["out"]
    assert _rm(r["calls"]) == ["network rm zzsvc-dev"], r["calls"]
    assert r["state"] == PLAY_NETS


def test_task_knob_never_recreates_shared_network(tmp_path):
    r = _run_play(tmp_path, {**PLAY_NETS, "cloud-public-dev": LIVE_OTHER}, knob=True)
    assert not _rm(r["calls"]), _rm(r["calls"])
    assert r["rc"] != 0, r["out"]
    assert r["state"]["cloud-public-dev"] == LIVE_OTHER


# --- static: no unguarded `docker network rm` anywhere -------------------------

def _rm_sites():
    sites = []
    for path in sorted(list((REPO / "v1").rglob("*")) + list((REPO / "v2").rglob("*"))):
        if not path.is_file() or "tests" in path.parts or path.suffix not in {".j2", ".yml", ".yaml", ".sh", ".py"}:
            continue
        lines = path.read_text(errors="replace").splitlines()
        for n, line in enumerate(lines):
            if re.search(r"\bnetwork\s+rm\b", line) and not line.lstrip().startswith("#"):
                sites.append(pytest.param(lines, n, id=f"{path.relative_to(REPO)}:{n + 1}"))
    return sites


RM_SITES = _rm_sites()


@pytest.mark.parametrize("lines,n", RM_SITES)
def test_network_rm_is_guarded(lines, n):
    line = lines[n]
    assert "|| true" not in line, "a failed network rm must not be swallowed: " + line.strip()
    window = "\n".join(lines[max(0, n - 12):n])
    assert "NETWORK_RECREATE_ON_MISMATCH" in window and "cloud-" in window, (
        "`docker network rm` must sit behind the NETWORK_RECREATE_ON_MISMATCH opt-in "
        "and the cloud-* refusal: " + line.strip()
    )
