"""Tests for tools/skstacks_deploy.py.

Every test drives the tool either in-process (`skstacks_deploy.main`) or as
a real subprocess (the `skstacks-deploy` shim), with fake `docker`,
`ansible-playbook` and `sk-lock` written to a `tmp_path/bin` placed first on
PATH -- the same pattern as
skstack06/tools/test_live_parity_sudo_fallback.py. Nothing here ever
touches a real docker daemon, ansible controller, or network beyond a
loopback HTTP server started by the test itself.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import skstacks_deploy as sd  # noqa: E402

SHIM = HERE / "skstacks-deploy"
MODULE = HERE / "skstacks_deploy.py"


# ---------------------------------------------------------------------------
# fixtures: a throwaway framework tree (probe.yml only) + instance dir
# ---------------------------------------------------------------------------

def _write_probe(framework_root: Path, service: str, probe: dict) -> None:
    d = framework_root / "v1" / "ansible" / "optional" / service
    d.mkdir(parents=True, exist_ok=True)
    (d / "probe.yml").write_text(yaml.safe_dump(probe))
    (d / f"deploy_{service}-prod.yml").write_text("# fake playbook\n")


@pytest.fixture
def framework_root(tmp_path):
    return tmp_path / "framework"


@pytest.fixture
def instance_dir(tmp_path):
    d = tmp_path / "instance"
    (d / "v1" / "ansible" / "shared").mkdir(parents=True)
    (d / "v1" / "ansible" / "shared" / "hosts").write_text("[managers]\n")
    return d


def _write_settings(instance_dir: Path, settings: dict) -> None:
    (instance_dir / "skstacks-deploy.yml").write_text(yaml.safe_dump(settings))


LOG = "FAKE_LOG"


def _logger_snippet(name: str) -> str:
    return f'echo "{name} $*" >> "${LOG}"\n'


def _write_fake(bindir: Path, name: str, body: str) -> None:
    f = bindir / name
    f.write_text("#!/usr/bin/env bash\n" + body)
    f.chmod(0o755)


def _base_env(tmp_path, bindir, extra=None):
    env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin",
           LOG: str(tmp_path / "calls.log")}
    if extra:
        env.update(extra)
    return env


def _calls(tmp_path) -> list[str]:
    log = tmp_path / "calls.log"
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if line.strip()]


def run_tool(tmp_path, bindir, args, framework_root=None, env_extra=None):
    extra = dict(env_extra or {})
    if framework_root is not None:
        extra["SKSTACKS_DEPLOY_FRAMEWORK_ROOT"] = str(framework_root)
    env = _base_env(tmp_path, bindir, extra)
    proc = subprocess.run(
        [sys.executable, str(MODULE), *args],
        env=env, capture_output=True, text=True,
    )
    return proc


# ---------------------------------------------------------------------------
# 1. dry run runs no subprocess
# ---------------------------------------------------------------------------

def test_dry_run_runs_no_subprocess(tmp_path, framework_root, instance_dir):
    _write_probe(framework_root, "skhub", {
        "services": ["nextcloud"], "mode": "swarm", "schema_changing": True,
        "http_checks": [],
    })
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    proc = run_tool(tmp_path, empty_bin, ["skhub", "--instance", str(instance_dir)],
                     framework_root=framework_root)
    assert proc.returncode == 0, proc.stderr
    assert "DRY RUN" in proc.stdout
    assert not (tmp_path / "calls.log").exists()


# ---------------------------------------------------------------------------
# 2. execute happy path runs steps in order
# ---------------------------------------------------------------------------

@pytest.fixture
def http_server(tmp_path):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _inspect_json(desired: int) -> str:
    return json.dumps([{"Spec": {"Mode": {"Replicated": {"Replicas": desired}}}}])


def _fake_docker_happy(bindir, desired=2):
    spec_json = _inspect_json(desired)
    _write_fake(bindir, "docker", f"""
{_logger_snippet("docker")}
if [ "$1 $2" = "service inspect" ]; then
  echo '{spec_json}'
  exit 0
fi
if [ "$1 $2" = "service ps" ]; then
  for i in $(seq 1 {desired}); do echo Running; done
  exit 0
fi
if [ "$1 $2 $3" = "service update --rollback" ]; then
  exit 0
fi
exit 1
""")


def _fake_ansible_ok(bindir):
    _write_fake(bindir, "ansible-playbook", _logger_snippet("ansible-playbook") + "exit 0\n")


def _fake_sklock_pass(bindir):
    # Mimics the real sk-lock contract closely enough for tests: logs, then
    # execs the wrapped command (everything after the first "--").
    _write_fake(bindir, "sk-lock", _logger_snippet("sk-lock") + """
args=("$@")
for i in "${!args[@]}"; do
  if [ "${args[$i]}" = "--" ]; then
    rest=("${args[@]:$((i+1))}")
    exec "${rest[@]}"
  fi
done
echo "fake sk-lock: no -- found" >&2
exit 64
""")


def test_execute_happy_path_runs_steps_in_order(tmp_path, framework_root, instance_dir, http_server):
    _write_probe(framework_root, "skhub", {
        "services": ["nextcloud"], "mode": "swarm", "schema_changing": False,
        "http_checks": [{"name": "status", "path": "/", "method": "GET", "expect_status": 200}],
    })
    _write_settings(instance_dir, {
        "snapshot_hook": "echo snapshot-ran-for-{purpose}",
        "services": {"skhub": {"db_dump_hook": "echo dbdump-ran-for-{service}",
                                "base_url": http_server}},
    })

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_docker_happy(bindir, desired=2)
    _fake_ansible_ok(bindir)
    _fake_sklock_pass(bindir)

    proc = run_tool(tmp_path, bindir, ["skhub", "--instance", str(instance_dir), "--execute"],
                     framework_root=framework_root)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    calls = _calls(tmp_path)
    names_in_order = [c.split()[0] for c in calls]
    # lock first, then ansible-playbook must come after at least one docker
    # (record) call, and docker calls happen both before and after
    # ansible-playbook (record, then verify).
    assert names_in_order[0] == "sk-lock"
    first_docker = names_in_order.index("docker")
    first_ansible = names_in_order.index("ansible-playbook")
    assert first_docker < first_ansible
    assert "docker" in names_in_order[names_in_order.index("ansible-playbook"):]

    record_file = instance_dir / ".skstacks-deploy"
    runs = list(record_file.glob("runs/*/records/nextcloud.json"))
    assert len(runs) == 1
    recorded = json.loads(runs[0].read_text())
    assert recorded["Spec"]["Mode"]["Replicated"]["Replicas"] == 2


# ---------------------------------------------------------------------------
# 3. failed probe restores only the failing service
# ---------------------------------------------------------------------------

def _fake_docker_one_failing(bindir, failing_name, desired=2):
    spec_json = _inspect_json(desired)
    _write_fake(bindir, "docker", f"""
{_logger_snippet("docker")}
if [ "$1 $2" = "service inspect" ]; then
  echo '{spec_json}'
  exit 0
fi
if [ "$1 $2" = "service ps" ]; then
  name="$3"
  running={desired}
  case "$name" in *{failing_name}*)
    [ -f "$RESTORED_MARKER" ] && running={desired} || running=$(({desired}-1))
    ;;
  esac
  for i in $(seq 1 $running); do echo Running; done
  exit 0
fi
if [ "$1 $2 $3" = "service update --rollback" ]; then
  touch "$RESTORED_MARKER"
  exit 0
fi
exit 1
""")


def test_failed_probe_restores_only_the_failing_service(tmp_path, framework_root, instance_dir):
    _write_probe(framework_root, "skhub", {
        "services": ["svc-a", "svc-b"], "mode": "swarm", "schema_changing": False,
        "http_checks": [],
    })
    _write_settings(instance_dir, {"snapshot_hook": "true"})

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_docker_one_failing(bindir, "svc-b", desired=2)
    _fake_ansible_ok(bindir)
    _fake_sklock_pass(bindir)

    restored_marker = tmp_path / "restored"
    proc = run_tool(tmp_path, bindir, ["skhub", "--instance", str(instance_dir), "--execute"],
                     framework_root=framework_root,
                     env_extra={"RESTORED_MARKER": str(restored_marker)})
    assert proc.returncode == 1, proc.stdout + proc.stderr

    calls = _calls(tmp_path)
    rollback_calls = [c for c in calls if "service update --rollback" in c]
    assert len(rollback_calls) == 1
    assert "svc-b" in rollback_calls[0]
    assert "svc-a" not in rollback_calls[0]
    assert restored_marker.exists()


# ---------------------------------------------------------------------------
# 4. lock contention runs nothing
# ---------------------------------------------------------------------------

def test_lock_contention_runs_nothing(tmp_path, framework_root, instance_dir):
    _write_probe(framework_root, "skhub", {
        "services": ["nextcloud"], "mode": "swarm", "schema_changing": False,
        "http_checks": [],
    })
    _write_settings(instance_dir, {"snapshot_hook": "true"})

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_fake(bindir, "sk-lock", _logger_snippet("sk-lock") + """
echo "sk-lock: LOCK TIMEOUT, command NOT run" >&2
exit 75
""")
    _fake_docker_happy(bindir)
    _fake_ansible_ok(bindir)

    proc = run_tool(tmp_path, bindir, ["skhub", "--instance", str(instance_dir), "--execute"],
                     framework_root=framework_root)
    assert proc.returncode == 75, proc.stdout + proc.stderr

    calls = _calls(tmp_path)
    assert [c.split()[0] for c in calls] == ["sk-lock"]
    assert not (instance_dir / ".skstacks-deploy").exists()


# ---------------------------------------------------------------------------
# 5. schema_changing app never auto-restores
# ---------------------------------------------------------------------------

def test_schema_changing_app_never_auto_restores(tmp_path, framework_root, instance_dir):
    _write_probe(framework_root, "skhub", {
        "services": ["svc-a", "svc-b"], "mode": "swarm", "schema_changing": True,
        "http_checks": [],
    })
    _write_settings(instance_dir, {"snapshot_hook": "true"})

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_docker_one_failing(bindir, "svc-b", desired=2)
    _fake_ansible_ok(bindir)
    _fake_sklock_pass(bindir)

    proc = run_tool(tmp_path, bindir, ["skhub", "--instance", str(instance_dir), "--execute"],
                     framework_root=framework_root,
                     env_extra={"RESTORED_MARKER": str(tmp_path / "restored")})
    assert proc.returncode == 3, proc.stdout + proc.stderr

    calls = _calls(tmp_path)
    assert not any("service update --rollback" in c for c in calls)
    assert "svc-b" in proc.stdout
    assert "recorded spec" in proc.stdout


# ---------------------------------------------------------------------------
# 6. snapshot hook failure aborts before the playbook
# ---------------------------------------------------------------------------

def test_snapshot_hook_failure_aborts_before_playbook(tmp_path, framework_root, instance_dir):
    _write_probe(framework_root, "skhub", {
        "services": ["nextcloud"], "mode": "swarm", "schema_changing": False,
        "http_checks": [],
    })
    _write_settings(instance_dir, {"snapshot_hook": "exit 1"})

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_docker_happy(bindir)
    _fake_ansible_ok(bindir)
    _fake_sklock_pass(bindir)

    proc = run_tool(tmp_path, bindir, ["skhub", "--instance", str(instance_dir), "--execute"],
                     framework_root=framework_root)
    assert proc.returncode == 3, proc.stdout + proc.stderr

    calls = _calls(tmp_path)
    assert not any(c.startswith("docker") for c in calls)
    assert not any(c.startswith("ansible-playbook") for c in calls)


# ---------------------------------------------------------------------------
# 7. missing snapshot hook fails closed
# ---------------------------------------------------------------------------

def test_missing_snapshot_hook_fails_closed(tmp_path, framework_root, instance_dir):
    _write_probe(framework_root, "skhub", {
        "services": ["nextcloud"], "mode": "swarm", "schema_changing": False,
        "http_checks": [],
    })
    _write_settings(instance_dir, {})  # no snapshot_hook, no opt-out

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_docker_happy(bindir)
    _fake_ansible_ok(bindir)
    _fake_sklock_pass(bindir)

    proc = run_tool(tmp_path, bindir, ["skhub", "--instance", str(instance_dir), "--execute"],
                     framework_root=framework_root)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    calls = _calls(tmp_path)
    assert not any(c.startswith("docker") for c in calls)
    assert not any(c.startswith("ansible-playbook") for c in calls)


# ---------------------------------------------------------------------------
# 8. compose mode never auto-restores
# ---------------------------------------------------------------------------

def test_compose_mode_never_auto_restores(tmp_path, framework_root, instance_dir):
    _write_probe(framework_root, "skfetch", {
        "services": ["svc-a"], "mode": "compose", "schema_changing": False,
        "http_checks": [],
    })
    _write_settings(instance_dir, {"snapshot_hook": "true"})

    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Ansible fails so the deploy itself is marked failed (compose mode has
    # no docker service inspect to key a replica mismatch off of).
    _write_fake(bindir, "ansible-playbook", _logger_snippet("ansible-playbook") + "exit 1\n")
    _write_fake(bindir, "docker", _logger_snippet("docker") + "exit 1\n")
    _fake_sklock_pass(bindir)

    proc = run_tool(tmp_path, bindir, ["skfetch", "--instance", str(instance_dir), "--execute"],
                     framework_root=framework_root)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    calls = _calls(tmp_path)
    assert not any("service update --rollback" in c for c in calls)


# ---------------------------------------------------------------------------
# 9. real probe.yml files parse and match the declared schema
# ---------------------------------------------------------------------------

REAL_FRAMEWORK_ROOT = HERE.parent


def test_probe_files_parse_and_match_declared_schema():
    probe_files = sorted(REAL_FRAMEWORK_ROOT.glob("v1/ansible/optional/*/probe.yml"))
    assert len(probe_files) >= 5, "expected the skhub/sksso/skstream/skfetch/skbook probes"
    for path in probe_files:
        probe = yaml.safe_load(path.read_text())
        sd.validate_probe(probe, path)  # raises DeployError on a schema violation
