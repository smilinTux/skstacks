"""skswarm-exporter: Swarm desired vs running state as Prometheus series."""
import json
import re
import threading
import urllib.error
import urllib.request

from skmon_support import FIXTURES

import skswarm_exporter as se
from skswarm_exporter import Exporter

T0 = se.parse_rfc3339("2026-11-12T00:00:00Z")
SWARM = FIXTURES / "swarm"


class FakeApi:
    """Serves the fixtures; `fail` makes every call raise OSError."""

    def __init__(self):
        self.fail = False

    def __call__(self, path):
        if self.fail:
            raise OSError("proxy down")
        return json.loads((SWARM / (path.lstrip("/") + ".json")).read_text()), 0.01


def fixture_api():
    return FakeApi()


def parse(text):
    """Exposition text -> {(name, sorted label tuple): value}."""
    out = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        m = re.fullmatch(r'([a-z_]+)(?:\{(.*)\})? (\S+)', line)
        labels = tuple(sorted(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', m.group(2) or "")))
        out[(m.group(1), labels)] = float(m.group(3))
    return out


def scrape(api, now=T0):
    return parse(Exporter(api, clock=lambda: now).render_after_poll())


def value(m, name, **labels):
    hits = [v for (n, ls), v in m.items() if n == name and all(dict(ls).get(k) == x for k, x in labels.items())]
    assert len(hits) == 1, (name, labels, hits)
    return hits[0]


def test_replicated_desired_and_running():
    m = scrape(fixture_api())
    assert m[("skswarm_service_replicas_desired", (("mode", "replicated"), ("service", "web"), ("stack", "app")))] == 2
    assert m[("skswarm_service_replicas_running", (("service", "web"), ("stack", "app")))] == 1


def test_global_desired_counts_tasks_desired_running():
    assert value(scrape(fixture_api()), "skswarm_service_replicas_desired", service="agent", mode="global") == 3


def test_parked_service_is_zero_zero():
    m = scrape(fixture_api())
    assert value(m, "skswarm_service_replicas_desired", service="parked") == 0 == value(m, "skswarm_service_replicas_running", service="parked")


def test_oldest_preparing_age():
    assert value(scrape(fixture_api(), now=T0 + 300), "skswarm_task_state_age_seconds", service="stuck", state="preparing") == 300


def test_no_age_series_when_none_stuck():
    assert not any(k[0] == "skswarm_task_state_age_seconds" and dict(k[1])["service"] == "web" for k in scrape(fixture_api()))


def test_failures_counted_once_per_task():
    e = Exporter(fixture_api(), clock=lambda: T0)
    e.poll()
    e.poll()
    assert value(parse(e.render()), "skswarm_service_task_failures_total", service="crashy") == 2


def test_failures_counter_is_monotonic_and_seen_set_pruned():
    tasks = json.loads((SWARM / "tasks.json").read_text())

    def api(path):
        return (tasks if path == "/tasks" else json.loads((SWARM / (path.lstrip("/") + ".json")).read_text())), 0.01

    e = Exporter(api, clock=lambda: T0)
    e.poll()
    tasks[:] = [t for t in tasks if t["ID"] != "t-crash1"]  # Docker pruned one failed task
    e.poll()
    assert value(parse(e.render()), "skswarm_service_task_failures_total", service="crashy") == 2
    assert "t-crash1" not in e._failed_seen


def test_node_info_and_up():
    m = scrape(fixture_api())
    assert value(m, "skswarm_node_info", node="n1", role="manager", availability="active", state="ready", addr="192.0.2.1") == 1
    assert value(m, "skswarm_up") == 1


def test_api_failure_keeps_last_values_and_sets_up_zero():
    api = fixture_api()
    e = Exporter(api, clock=lambda: T0)
    e.poll()
    api.fail = True
    e.poll()
    m = parse(e.render())
    assert value(m, "skswarm_up") == 0 and value(m, "skswarm_service_replicas_running", service="web") == 1
    assert ("skswarm_api_duration_seconds", (("endpoint", "/services"),)) in m


def test_job_modes_are_skipped():
    def api(path):
        if path == "/services":
            return [{"ID": "j", "Spec": {"Name": "app_job", "Labels": {}, "Mode": {"ReplicatedJob": {}}}}], 0.01
        return [], 0.01

    m = parse(Exporter(api, clock=lambda: T0).render_after_poll())
    assert not any(dict(k[1]).get("service") == "app_job" for k in m)
    assert value(m, "skswarm_up") == 1


def test_output_is_valid_exposition_format():
    text = Exporter(fixture_api(), clock=lambda: T0).render_after_poll()
    assert "# HELP skswarm_up" in text and "# TYPE skswarm_service_task_failures_total counter" in text
    for line in text.splitlines():
        assert line.startswith("#") or re.fullmatch(r'[a-z_]+(\{[^}]*\})? -?[0-9.e+]+', line)


def test_label_values_are_escaped():
    assert se.escape('a\\b"c\nd') == 'a\\\\b\\"c\\nd'

    def api(path):
        if path == "/nodes":
            return [{"ID": "x", "Description": {"Hostname": 'we"ird\\n\nname'}, "Spec": {}, "Status": {}}], 0.01
        return [], 0.01

    text = Exporter(api, clock=lambda: T0).render_after_poll()
    assert 'node="we\\"ird\\\\n\\nname"' in text
    assert len([l for l in text.splitlines() if l.startswith("skswarm_node_info")]) == 1


def test_unknown_path_is_404_and_metrics_is_200():
    srv = se.make_server(lambda: "skswarm_up 1\n", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        try:
            urllib.request.urlopen(base + "/nope")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as err:
            assert err.code == 404
        assert urllib.request.urlopen(base + "/metrics").read() == b"skswarm_up 1\n"
    finally:
        srv.shutdown()
        srv.server_close()


def test_env_defaults():
    assert se.parse_env({}) == {"host": "http://socket-proxy:2375", "poll_s": 15.0, "port": 9323, "timeout_s": 10.0}
    assert se.parse_env({"SKSWARM_PORT": "1", "SKSWARM_DOCKER_HOST": "http://x:1"})["port"] == 1
