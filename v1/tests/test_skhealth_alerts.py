"""skhealth dedupe and recovery state transitions (the pure transition()
function), and the whole run end to end: the real engine as a subprocess
with a timer's minimal PATH, stub commands, and a local HTTP server standing
in for the Telegram API and the webhook."""
import json
import subprocess
import sys

import pytest

from skhealth_support import ENGINE, FakeHost, Hook, env_without_path, load_engine

eng = load_engine()
H = 3600.0


def step(state, problems, now, ran=("host",), disabled=(), confirm=2, realert=6):
    return eng.transition(state, problems, set(ran), set(disabled), now, confirm, realert)


def kinds(events):
    return [(e["kind"], e["key"]) for e in events]


# --- transition() ---------------------------------------------------------------

def test_problem_alerts_after_confirm_runs_then_stays_quiet():
    p = {"host.unit.x.service": "systemd unit x.service failed"}
    s, ev = step({}, p, 0)
    assert ev == [] and s["host.unit.x.service"]["count"] == 1 and not s["host.unit.x.service"]["alerted"]
    s, ev = step(s, p, 300)
    assert kinds(ev) == [("problem", "host.unit.x.service")]
    for t in (600, 900, 3 * H):
        s, ev = step(s, p, t)
        assert ev == [], t


def test_realert_every_n_hours_while_it_persists():
    p = {"host.load": "load high"}
    s, _ = step({}, p, 0)
    s, ev = step(s, p, 300)
    assert kinds(ev) == [("problem", "host.load")]
    s, ev = step(s, p, 300 + 6 * H - 1)
    assert ev == []
    s, ev = step(s, p, 300 + 6 * H)
    assert kinds(ev) == [("still", "host.load")] and ev[0]["message"] == "load high (for 6h)"
    s, ev = step(s, p, 300 + 7 * H)
    assert ev == []
    s, ev = step(s, p, 300 + 12 * H)
    assert kinds(ev) == [("still", "host.load")]


def test_realert_zero_means_never():
    p = {"host.load": "x"}
    s, _ = step({}, p, 0, realert=0)
    s, _ = step(s, p, 1, realert=0)
    s, ev = step(s, p, 100 * H, realert=0)
    assert ev == []


def test_recovered_once_after_an_alert():
    p = {"host.load": "load high"}
    s, _ = step({}, p, 0)
    s, _ = step(s, p, 300)
    s, ev = step(s, {}, 600)
    assert kinds(ev) == [("recovered", "host.load")] and ev[0]["message"] == "load high"
    assert s == {}
    s, ev = step(s, {}, 900)
    assert ev == []


def test_a_blip_that_never_alerted_recovers_silently():
    s, ev = step({}, {"host.load": "x"}, 0)
    s, ev = step(s, {}, 300)
    assert ev == [] and s == {}


def test_confirm_runs_one_alerts_at_once():
    s, ev = step({}, {"host.load": "x"}, 0, confirm=1)
    assert kinds(ev) == [("problem", "host.load")]


def test_oom_is_an_event_alerted_at_once_and_never_recovered():
    s, ev = step({}, {"host.oom": "OOM killer fired 1 time(s)"}, 0)
    assert kinds(ev) == [("event", "host.oom")] and "host.oom" not in s
    s, ev = step(s, {}, 300)
    assert ev == []


def test_a_check_that_did_not_run_keeps_its_problems():
    p = {"swarm.replicas.app": "service app 0/1"}
    s, _ = step({}, p, 0, ran=("swarm",))
    s, _ = step(s, p, 300, ran=("swarm",))
    # next run this host is no longer the primary: swarm did not run
    s2, ev = step(s, {}, 600, ran=("host",))
    assert ev == [] and s2["swarm.replicas.app"] == s["swarm.replicas.app"]
    # the check itself crashed: no verdict on its keys either
    s3, ev = step(s, {"check.swarm": "check swarm failed"}, 600, ran=("host",))
    assert "swarm.replicas.app" in s3 and kinds(ev) == []


def test_a_disabled_check_drops_its_problems_silently():
    p = {"disk./dev/sda": "SMART FAILED"}
    s, _ = step({}, p, 0, ran=("disks",))
    s, _ = step(s, p, 300, ran=("disks",))
    s, ev = step(s, {}, 600, ran=("host",), disabled=("disks",))
    assert ev == [] and s == {}


def test_several_changes_in_one_run_are_one_batch():
    s, _ = step({}, {"host.load": "a", "host.memory": "b"}, 0)
    s, _ = step(s, {"host.load": "a", "host.memory": "b"}, 300)
    s, ev = step(s, {"host.memory": "b", "host.fs./": "c"}, 600)
    assert kinds(ev) == [("recovered", "host.load")]
    s, ev = step(s, {"host.memory": "b", "host.fs./": "c"}, 900)
    assert kinds(ev) == [("problem", "host.fs./")]


def test_format_names_cluster_and_host():
    text = eng.format_text("prod-a", "mgr-1", [{"kind": "problem", "key": "k", "message": "m"},
                                               {"kind": "recovered", "key": "r", "message": "n"}])
    assert text == "[skhealth] prod-a / mgr-1\nPROBLEM: m\nRECOVERED: n"


# --- end to end: the real engine, stub commands, a fake Telegram + webhook --------

TOKEN = "123456:example-telegram-token-not-a-secret"
CHAT = "-100987654321"


@pytest.fixture
def hook():
    h = Hook()
    yield h
    h.close()


@pytest.fixture
def host(tmp_path, hook):
    h = FakeHost(tmp_path)
    h.cfg["telegram_api_base"] = hook.url
    h.save()
    h.secrets.write_text(f"SKHEALTH_TELEGRAM_TOKEN='{TOKEN}'\nSKHEALTH_TELEGRAM_CHAT_ID='{CHAT}'\n"
                         f"SKHEALTH_WEBHOOK_URL='{hook.url}/hook/secret-path-token'\n")
    return h


def skhealth(host, *args, now=None):
    if now is not None:
        host.cfg["now"] = now
        host.save()
    proc = subprocess.run([sys.executable, str(ENGINE), "--config", str(host.config_path), *args],
                          capture_output=True, text=True, env=env_without_path(), timeout=60)
    out = proc.stdout + proc.stderr
    assert TOKEN not in out and CHAT not in out and "secret-path-token" not in out, "a secret was printed"
    return proc


def webhook_events(hook):
    return [e for p in hook.posts if p["path"].startswith("/hook/") for e in json.loads(p["body"])["events"]]


def telegram_posts(hook):
    return [p for p in hook.posts if p["path"] == f"/bot{TOKEN}/sendMessage"]


def test_injected_failure_gives_exactly_one_alert_and_one_recovery(host, hook):
    fail = "skhealth-gate-fail.service loaded failed failed injected\n"
    assert skhealth(host, now=1000).returncode == 0
    assert hook.posts == []
    host.on("systemctl", ["--failed"], fail)
    skhealth(host, now=1300)                      # seen once: not yet
    assert hook.posts == []
    r = skhealth(host, now=1600)                  # seen twice: alert
    assert "PROBLEM: systemd unit skhealth-gate-fail.service failed" in r.stdout
    skhealth(host, now=1900)                      # still failing: quiet
    ev = webhook_events(hook)
    assert [(e["kind"], e["key"]) for e in ev] == [("problem", "host.unit.skhealth-gate-fail.service")]
    tg = telegram_posts(hook)
    assert len(tg) == 1 and "chat_id=" in tg[0]["body"]
    assert "%5Bskhealth%5D+lab+%2F+node-a" in tg[0]["body"]   # names cluster and host
    host.clear("systemctl")
    skhealth(host, now=2200)                      # cleared: one recovery
    skhealth(host, now=2500)
    ev = webhook_events(hook)
    assert [(e["kind"], e["key"]) for e in ev] == [("problem", "host.unit.skhealth-gate-fail.service"),
                                                   ("recovered", "host.unit.skhealth-gate-fail.service")]
    assert len(telegram_posts(hook)) == 2
    body = json.loads([p for p in hook.posts if p["path"].startswith("/hook/")][-1]["body"])
    assert body["cluster"] == "lab" and body["host"] == "node-a" and body["source"] == "skhealth"


def test_undelivered_alert_is_retried_next_run(host, hook):
    host.on("systemctl", ["--failed"], "x.service loaded failed failed X\n")
    skhealth(host, now=1000)
    hook.set_status(500)
    r = skhealth(host, now=1300)
    assert "alert NOT delivered (telegram: HTTP 500; webhook: HTTP 500)" in r.stderr
    hook.set_status(200)
    hook.posts.clear()
    skhealth(host, now=1600)
    assert [(e["kind"]) for e in webhook_events(hook)] == ["problem"]


def test_test_mode_sends_one_message_per_channel(host, hook):
    r = skhealth(host, "--test")
    assert r.returncode == 0 and "sent through 2/2 channel(s)" in r.stdout
    assert len(telegram_posts(hook)) == 1
    assert [e["kind"] for e in webhook_events(hook)] == ["test"]
    assert not (host.state / "state.json").exists(), "--test must not touch the state"


def test_test_mode_fails_loudly_without_channels(host):
    host.secrets.write_text("")
    r = skhealth(host, "--test")
    assert r.returncode == 1 and "no alert channel configured" in r.stderr


def test_test_mode_reports_a_failed_channel_without_printing_it(host, hook):
    host.secrets.write_text(f"SKHEALTH_WEBHOOK_URL='http://127.0.0.1:1/secret-path-token'\n"
                            f"SKHEALTH_TELEGRAM_TOKEN='{TOKEN}'\nSKHEALTH_TELEGRAM_CHAT_ID='{CHAT}'\n")
    r = skhealth(host, "--test")
    assert r.returncode == 1 and "1/2" in r.stdout and "webhook: URLError" in r.stdout


def test_check_mode_prints_problems_and_changes_nothing(host, hook):
    host.on("systemctl", ["--failed"], "x.service loaded failed failed X\n")
    r = skhealth(host, "check", now=1000)
    assert "host.unit.x.service: systemd unit x.service failed" in r.stdout
    assert hook.posts == [] and not (host.state / "state.json").exists()


def test_status_shows_channels_as_booleans_only(host):
    host.on("systemctl", ["--failed"], "x.service loaded failed failed X\n")
    skhealth(host, now=1000)
    st = json.loads(skhealth(host, "status").stdout)
    assert st["telegram"] is True and st["webhook"] is True
    assert st["problems"]["host.unit.x.service"]["count"] == 1


def test_state_file_is_root_only(host):
    skhealth(host, now=1000)
    assert (host.state / "state.json").stat().st_mode & 0o777 == 0o600


def test_a_second_run_holding_the_lock_skips(host):
    import fcntl
    with open(host.state / "lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        r = skhealth(host, now=1000)
    assert r.returncode == 0 and "holds the lock" in r.stdout


def test_summary_line_reports_the_runs_own_cost(host):
    """README "Resource cost" is backed by this line: every run and check
    prints its CPU seconds and peak RSS (the lab stage records it)."""
    import re
    for args in ((), ("check",)):
        out = skhealth(host, *args, now=1000).stdout.splitlines()[-1]
        m = re.search(r"cpu (\d+\.\d\d)s, peak RSS (\d+\.\d) MiB \(largest command (\d+\.\d) MiB\); checks run: ", out)
        assert m, out
        assert 1.0 < float(m.group(2)) < 128.0, "the engine's own RSS, under the unit's MemoryMax"
