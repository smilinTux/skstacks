"""skmon-bridge (F6): Alertmanager webhook to sk-alert argv, delivery status codes, healthz, metrics, logs."""
import json
import logging
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from skmon_support import get, load, post, server, server_failing, server_slow  # noqa: F401 (fixtures)

import skmon_bridge as sb
from skmon_bridge import Config, State, build_call, healthz, level_for, target_of, thread_key

CFG = Config(estate="example", grafana_url="https://skmon.example.test", skalert="/opt/sk-alert/sk-alert",
             talk=True, alertmanager_url="http://127.0.0.1:1")


def test_firing_maps_to_sk_alert_args():
    a = build_call(load("am_firing.json"), CFG, muted=14)
    assert a[:3] == [CFG.skalert, "-l", "crit"]
    assert a[a.index("-g") + 1] == thread_key("example", load("am_firing.json")["groupKey"])
    assert a[a.index("--title") + 1] == "NfsMountUnresponsive on worker-2"
    assert "--talk" in a
    body = a[-1]
    assert "14 dependent alerts muted." in body and "First check:" in body
    assert "https://skmon.example.test/d/estate-overview" in body
    assert body.splitlines()[0].startswith("Shared storage is not answering")


def test_talk_comes_right_after_level_and_thread():
    a = build_call(load("am_firing.json"), CFG, None)
    assert a[a.index("-g") + 2] == "--talk"


def test_thread_key_shape():
    k = thread_key("example", "gk")
    assert k.startswith("skmon/example/") and len(k.split("/")[2]) == 16


def test_resolved_uses_same_thread_and_resolve_flag():
    a = build_call(load("am_resolved.json"), CFG, None)
    assert a[a.index("-g") + 1] == thread_key("example", load("am_resolved.json")["groupKey"])
    assert a[a.index("--resolve") + 1].startswith("NfsMountUnresponsive resolved after ")
    assert a[-1] == "NfsMountUnresponsive resolved after 4m12s"
    assert "--title" not in a


@pytest.mark.parametrize("start,end,text", [
    ("2026-11-12T10:00:00Z", "2026-11-12T11:03:20Z", "1h3m"),
    ("2026-11-12T10:00:00Z", "2026-11-12T10:00:09Z", "9s"),
])
def test_resolve_duration_formats(start, end, text):
    p = load("am_resolved.json")
    p["alerts"][0].update(startsAt=start, endsAt=end)
    assert build_call(p, CFG, None)[-1] == f"NfsMountUnresponsive resolved after {text}"


@pytest.mark.parametrize("ends", ["", "not-a-time", "0001-01-01T00:00:00Z"])
def test_resolve_without_usable_times(ends):
    p = load("am_resolved.json")
    p["alerts"][0]["endsAt"] = ends
    assert build_call(p, CFG, None)[-1] == "NfsMountUnresponsive resolved"


@pytest.mark.parametrize("sev,lvl", [("critical", "crit"), ("warning", "warn"), ("none", "info"), ("", "info")])
def test_level_mapping(sev, lvl):
    assert level_for(sev) == lvl


def test_title_fallback_chain():
    assert target_of({"node": "n", "instance": "i"}) == "n"
    assert target_of({"instance": "i"}) == "i" and target_of({"service": "s"}) == "s"
    assert target_of({"job": "j"}) == "j" and target_of({"estate": "e"}) == "e"


def test_title_falls_back_to_first_alert_labels():
    p = load("am_firing.json")
    p["commonLabels"] = {}
    a = build_call(p, CFG, None)
    assert a[a.index("--title") + 1] == "NfsMountUnresponsive on worker-2"
    assert a[2] == "crit"


def test_body_capped():
    body = build_call(load("am_big_group.json"), CFG, None)[-1]
    assert len(body) <= CFG.body_max and body.endswith(" (truncated)")


def test_big_group_adds_count_line():
    p = load("am_big_group.json")
    p["commonAnnotations"]["summary"] = "short"
    body = build_call(p, CFG, None)[-1]
    assert body.splitlines()[1] == "(60 alerts in this group)"


def test_body_is_last_argv_element_and_not_truncated_when_short():
    a = build_call(load("am_firing.json"), CFG, None)
    assert a[-1].startswith("Shared storage") and "(truncated)" not in a[-1]


def test_never_uses_dedup_key_flag():
    assert "-k" not in build_call(load("am_firing.json"), CFG, None)


def test_talk_omitted_when_not_configured():
    assert "--talk" not in build_call(load("am_firing.json"), replace(CFG, talk=False), None)


def test_muted_count_omitted_when_query_fails():
    assert "dependent alerts muted" not in build_call(load("am_firing.json"), CFG, None)[-1]
    assert "dependent alerts muted" not in build_call(load("am_firing.json"), CFG, 0)[-1]


def test_muted_count_counts_this_estate_only():
    seen = []

    class AM(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = json.dumps([{"labels": {"estate": "example"}}, {"labels": {"estate": "example"}},
                               {"labels": {"estate": "other"}}]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), AM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cfg = replace(CFG, alertmanager_url=f"http://127.0.0.1:{srv.server_address[1]}")
        assert sb.muted_count(cfg) == 2
        assert seen == ["/api/v2/alerts?active=false&silenced=false&inhibited=true"]
    finally:
        srv.shutdown()
        srv.server_close()
    assert sb.muted_count(CFG) is None


def test_alert_success_runs_sk_alert_once(server):
    assert post(server, "/alert", load("am_firing.json")).status == 200
    calls = server.calls()
    assert len(calls) == 1 and "--talk" in calls[0] and calls[0][-1].endswith("/d/estate-overview")
    assert server.state.deliveries == 1 and server.state.last_delivery_ok


def test_nonzero_exit_returns_500(server_failing):
    assert post(server_failing, "/alert", load("am_firing.json")).status == 500
    assert server_failing.state.failures == 1 and not server_failing.state.last_delivery_ok


def test_skalert_timeout_returns_500(server_slow):
    assert post(server_slow, "/alert", load("am_firing.json")).status == 500
    assert server_slow.state.failures == 1


def test_success_after_failure_recovers_delivery_flag(server):
    server.state.last_delivery_ok = False
    assert post(server, "/alert", load("am_resolved.json")).status == 200
    assert server.state.last_delivery_ok


def test_watchdog_records_time_and_never_calls_sk_alert(server):
    assert server.state.last_watchdog is None
    assert post(server, "/watchdog", load("am_watchdog.json")).status == 200
    assert server.state.last_watchdog is not None and server.calls() == []
    assert get(server, "/healthz", main=False).status == 200


def test_healthz_logic():
    assert healthz(State(last_watchdog=1000, last_delivery_ok=True), 1179)[0] == 200
    assert healthz(State(last_watchdog=1000, last_delivery_ok=True), 1181)[0] != 200
    assert healthz(State(last_watchdog=1000, last_delivery_ok=False), 1001)[0] != 200
    assert healthz(State(last_watchdog=None, last_delivery_ok=True), 0)[0] != 200


def test_state_defaults():
    s = State()
    assert s.last_watchdog is None and s.last_delivery_ok is True and s.deliveries == s.failures == 0


def test_healthz_only_on_healthz_port(server):
    assert get(server, "/healthz", main=True).status == 404
    assert get(server, "/healthz", main=False).status == 503
    assert get(server, "/metrics", main=False).status == 404
    assert post(server, "/alert", load("am_firing.json"), main=False).status == 404


def test_metrics_text(server):
    post(server, "/watchdog", load("am_watchdog.json"))
    r = get(server, "/metrics")
    text = r.body.decode()
    assert r.status == 200
    for name, kind in (("skmon_bridge_deliveries_total", "counter"),
                       ("skmon_bridge_delivery_failures_total", "counter"),
                       ("skmon_bridge_last_watchdog_timestamp_seconds", "gauge")):
        assert f"# HELP {name} " in text and f"# TYPE {name} {kind}" in text
    assert "skmon_bridge_last_watchdog_timestamp_seconds 0\n" not in text


def test_unknown_path_404_and_wrong_method_405(server):
    assert get(server, "/nope").status == 404
    assert get(server, "/alert").status == 405
    assert post(server, "/metrics", {}).status == 405


def test_invalid_json_400(server):
    assert post(server, "/alert", None, raw=b"{not json").status == 400
    assert post(server, "/alert", None, raw=b"[1, 2]").status == 400
    assert server.calls() == []


def test_oversize_body_413(server):
    assert post(server, "/alert", None, raw=b" " * (1024 * 1024 + 1)).status == 413
    assert server.calls() == []


def test_no_body_or_secret_in_logs(server, caplog):
    caplog.set_level(logging.DEBUG)
    post(server, "/alert", load("am_firing.json"))
    assert "Shared storage is not answering" not in caplog.text and "SECRET-MARKER" not in caplog.text
    assert "alertname=NfsMountUnresponsive status=firing exit=0" in caplog.text


def test_no_secret_in_logs_on_failure(server_failing, caplog):
    caplog.set_level(logging.DEBUG)
    post(server_failing, "/alert", load("am_firing.json"))
    assert "SECRET-MARKER" not in caplog.text and "First check" not in caplog.text
    assert "exit=1" in caplog.text


def test_config_from_env():
    cfg = sb.config_from_env({"SKMON_ESTATE": "e", "SKMON_GRAFANA_URL": "https://g.example.test/", "SKMON_TALK": "1",
                              "SKMON_AM_URL": "http://am:9093", "SKALERT_CONFIG_DIR": "/c"})
    assert (cfg.estate, cfg.grafana_url, cfg.talk, cfg.skalert) == ("e", "https://g.example.test", True, "/opt/sk-alert/sk-alert")
    assert cfg.config_dir == "/c" and cfg.state_dir == "/state"
    assert sb.config_from_env({}).talk is False
