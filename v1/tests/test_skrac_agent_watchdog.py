"""skrac agent watchdog: playbook shape, and the script's decisions driven with stub tools."""
import os
import pathlib
import stat
import subprocess

import pytest
import yaml

SKRAC = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skrac"
SRC = SKRAC / "src/agent-watchdog"
SCRIPT = SRC / "skrac-agent-watchdog.sh"
PLAYBOOK = SKRAC / "deploy_skrac-agent-watchdog.yml"


def _stub(bin_dir, name, body):
    p = bin_dir / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path, *, lastrcv_ms=None, ticks=(100, 100), uptime=10_000, started_s=1_000, runs=1, env=None):
    """Run the watchdog `runs` times for one fake agent unit; return (outputs, restarts)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True, exist_ok=True)
    (proc / "uptime").write_text(f"{uptime}.00 1.00\n")
    restarts = tmp_path / "restarts"
    restarts.write_text("")
    sock = ""
    if lastrcv_ms is not None:
        sock = (
            'echo \'0 0 10.0.0.1:50000 203.0.113.1:443 users:(("meshagent",pid=4242,fd=7))\'\n'
            f"echo ' cubic rto:204 lastsnd:{lastrcv_ms} lastrcv:{lastrcv_ms} lastack:{lastrcv_ms}'\n"
        )
    _stub(b, "ss", sock)
    _stub(b, "logger", "true\n")
    clock = tmp_path / "clock"
    clock.write_text("1000000")
    # each run is one timer period (180 s) after the previous one
    _stub(b, "date", f"""
if [ "$1" = "+%s" ]; then n=$(cat {clock}); echo $n; echo $((n + 180)) > {clock}; else exec /bin/date "$@"; fi
""")
    _stub(b, "systemctl", f"""
case "$*" in
  *MainPID*) echo 4242 ;;
  *ActiveEnterTimestampMonotonic*) echo {started_s * 1_000_000} ;;
  restart*) echo "$2" >> {restarts} ;;
esac
""")
    outs = []
    for i in range(runs):
        t = ticks[min(i, len(ticks) - 1)]
        (proc / "4242/stat").write_text("4242 (meshagent) S " + " ".join(["0"] * 10) + f" {t} 0 0 0\n")
        e = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", PROC=str(proc), STATE_DIR=str(tmp_path / "state"),
                 UNITS="meshagent.service", **(env or {}))
        r = subprocess.run(["bash", str(SCRIPT)], env=e, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        outs.append(r.stdout)
    return outs, restarts.read_text().split()


def test_script_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_fresh_session_is_left_alone(tmp_path):
    outs, restarts = _run(tmp_path, lastrcv_ms=30_000, runs=3)
    assert restarts == [] and not any(outs)


def test_stale_session_restarts(tmp_path):
    outs, restarts = _run(tmp_path, lastrcv_ms=700_000)
    assert restarts == ["meshagent.service"]
    assert "nothing received from the server for 700 s" in outs[0]


def test_no_connection_needs_consecutive_checks(tmp_path):
    outs, restarts = _run(tmp_path / "one", lastrcv_ms=None, runs=1)
    assert restarts == []
    outs, restarts = _run(tmp_path / "two", lastrcv_ms=None, runs=2)
    assert restarts == ["meshagent.service"] and "no server connection for 2 checks" in outs[-1]


def test_busy_but_not_spinning_is_left_alone(tmp_path):
    hz = int(subprocess.check_output(["getconf", "CLK_TCK"]))
    # 30% of a core (a desktop session's capture runs in a child, not here)
    outs, restarts = _run(tmp_path, lastrcv_ms=30_000, ticks=(0, 54 * hz, 108 * hz, 162 * hz), runs=4)
    assert restarts == []


def test_young_agent_is_left_alone(tmp_path):
    # started 100 s ago (uptime 1100, started at 1000): below MIN_UPTIME_S
    outs, restarts = _run(tmp_path, lastrcv_ms=700_000, uptime=1_100, started_s=1_000)
    assert restarts == []


def test_spin_on_the_main_process_restarts(tmp_path):
    hz = int(subprocess.check_output(["getconf", "CLK_TCK"]))
    # 170 s of CPU per 180 s run: 94% of a core, twice in a row
    outs, restarts = _run(tmp_path, lastrcv_ms=30_000, ticks=(0, 170 * hz, 340 * hz), runs=3)
    assert restarts == ["meshagent.service"] and "CPU for 2 checks" in outs[-1]


def test_playbook_shape():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    tasks = {t["name"]: t for t in plays[0]["tasks"]}
    assert plays[0]["hosts"] == "{{ skrac_watchdog_hosts | default('skrac_agents') }}"
    dropin = tasks["Agent units restart always and stop within the timeout"]["copy"]["content"]
    assert "Restart=always" in dropin and "TimeoutStopSec=" in dropin
    assert tasks["Enable and start the watchdog timer"]["systemd"]["name"] == "skrac-agent-watchdog.timer"
    assert "OnUnitActiveSec=3min" in (SRC / "skrac-agent-watchdog.timer").read_text()
    assert "EnvironmentFile=-/etc/default/skrac-agent-watchdog" in (SRC / "skrac-agent-watchdog.service").read_text()


def test_no_credentials_needed():
    body = SCRIPT.read_text()
    for word in ("meshctrl", "loginpass", "password", "token"):
        assert word not in body.lower()
