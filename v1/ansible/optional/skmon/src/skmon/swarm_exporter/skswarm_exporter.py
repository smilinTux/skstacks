#!/usr/bin/env python3
"""skswarm-exporter: Docker Swarm desired-vs-running state as Prometheus metrics.

Polls GET /services, /tasks, /nodes and /info (unversioned paths) through a read-only
socket proxy and serves the last render on GET /metrics. Only /metrics answers 200.
A scrape never waits on a poll: the poll runs in its own thread.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping

log = logging.getLogger("skswarm_exporter")

Api = Callable[[str], "tuple[Any, float]"]

ENDPOINTS = ("/services", "/tasks", "/nodes", "/info")
STACK_LABEL = "com.docker.stack.namespace"
FAILED_STATES = ("failed", "rejected")
AGE_STATES = ("new", "pending", "assigned", "accepted", "preparing", "starting")

HELP = {
    "skswarm_service_replicas_desired": ("gauge", "Desired replicas per service (replicated: spec, global: tasks desired running)."),
    "skswarm_service_replicas_running": ("gauge", "Tasks desired running and currently running per service."),
    "skswarm_task_state_age_seconds": ("gauge", "Age of the oldest task (desired running) stuck in a pre-running state."),
    "skswarm_service_task_failures_total": ("counter", "Tasks observed in failed or rejected state, each counted once."),
    "skswarm_node_info": ("gauge", "Swarm node, value 1."),
    "skswarm_api_duration_seconds": ("gauge", "Duration of the last Docker API call per endpoint."),
    "skswarm_up": ("gauge", "1 when the last poll fully succeeded."),
}


def parse_rfc3339(ts: str) -> float:
    """Parse an RFC3339 timestamp with optional nanoseconds and Z into epoch seconds."""
    s = ts.strip()
    tz = timezone.utc
    if s.endswith(("Z", "z")):
        s = s[:-1]
    elif len(s) > 6 and s[-6] in "+-" and s[-3] == ":":
        sign = 1 if s[-6] == "+" else -1
        from datetime import timedelta
        tz = timezone(sign * timedelta(hours=int(s[-5:-3]), minutes=int(s[-2:])))
        s = s[:-6]
    date, _, frac = s.partition(".")
    micro = int((frac + "000000")[:6]) if frac else 0
    dt = datetime.strptime(date, "%Y-%m-%dT%H:%M:%S").replace(microsecond=micro, tzinfo=tz)
    return dt.timestamp()


def escape(v: str) -> str:
    """Escape a label value per the exposition format."""
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(pairs: "tuple[tuple[str, str], ...]") -> str:
    return "{" + ",".join(f'{k}="{escape(v)}"' for k, v in pairs) + "}" if pairs else ""


def _split(svc: dict) -> "tuple[str, str]":
    """(stack, service) with the '<stack>_' prefix removed from the service name."""
    name = svc.get("Spec", {}).get("Name", "")
    stack = svc.get("Spec", {}).get("Labels", {}).get(STACK_LABEL, "")
    if stack and name.startswith(stack + "_"):
        name = name[len(stack) + 1:]
    return stack, name


class Exporter:
    def __init__(self, api: Api, clock: Callable[[], float]) -> None:
        self.api = api
        self.clock = clock
        self.up = 0
        self.durations: dict[str, float] = {}
        # series name -> list of (label pairs, value); replaced wholesale on a good poll.
        self.series: dict[str, list] = {}
        self._failed_seen: set[str] = set()
        self._failures: dict[tuple[str, str], int] = {}

    def poll(self) -> None:
        """Fetch all endpoints; on any failure set up to 0 and keep the last good values."""
        data: dict[str, Any] = {}
        ok = True
        for ep in ENDPOINTS:
            try:
                body, secs = self.api(ep)
            except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
                log.warning("docker api %s failed: %s", ep, type(exc).__name__)
                ok = False
                continue
            self.durations[ep] = secs
            data[ep] = body
        if not ok:
            self.up = 0
            return
        try:
            self._compute(data["/services"], data["/tasks"], data["/nodes"])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            log.warning("unexpected docker api shape: %s", type(exc).__name__)
            self.up = 0
            return
        self.up = 1

    def _compute(self, services: list, tasks: list, nodes: list) -> None:
        now = self.clock()
        by_id = {s["ID"]: s for s in services}
        per: dict[str, list] = {sid: [] for sid in by_id}
        for t in tasks:
            if t.get("ServiceID") in per:
                per[t["ServiceID"]].append(t)

        # Failure counter: a task ID counts once. The seen set is pruned to the IDs still
        # listed so it stays bounded; totals never decrease. Edge case: if Docker's task
        # history pruned an ID and it later reappeared in the listing it would count again,
        # which does not happen in normal operation (a pruned task is gone for good).
        listed = {t["ID"] for t in tasks}
        for sid, svc in by_id.items():
            key = _split(svc)
            self._failures.setdefault(key, 0)
            for t in per[sid]:
                if t.get("Status", {}).get("State") in FAILED_STATES and t["ID"] not in self._failed_seen:
                    self._failed_seen.add(t["ID"])
                    self._failures[key] += 1
        self._failed_seen &= listed

        desired, running, ages, fails = [], [], [], []
        for sid, svc in by_id.items():
            stack, name = _split(svc)
            mode = svc.get("Spec", {}).get("Mode", {})
            want = [t for t in per[sid] if t.get("DesiredState") == "running"]
            if "Replicated" in mode:
                mode_name, n = "replicated", int(mode["Replicated"].get("Replicas", 0))
            elif "Global" in mode:
                mode_name, n = "global", len(want)
            else:
                continue  # replicated-job and global-job: run-to-completion, no steady state to compare
            base = (("stack", stack), ("service", name))
            desired.append(((("stack", stack), ("service", name), ("mode", mode_name)), n))
            running.append((base, sum(1 for t in want if t.get("Status", {}).get("State") == "running")))
            fails.append((base, self._failures[(stack, name)]))
            oldest: dict[str, float] = {}
            for t in want:
                st = t.get("Status", {})
                state = st.get("State")
                if state in AGE_STATES and st.get("Timestamp"):
                    ts = parse_rfc3339(st["Timestamp"])
                    oldest[state] = min(ts, oldest.get(state, ts))
            for state in AGE_STATES:
                if state in oldest:
                    ages.append(((("stack", stack), ("service", name), ("state", state)), max(0.0, now - oldest[state])))

        info = []
        for n in nodes:
            info.append(((
                ("node", n.get("Description", {}).get("Hostname", "")),
                ("role", n.get("Spec", {}).get("Role", "")),
                ("availability", n.get("Spec", {}).get("Availability", "")),
                ("state", n.get("Status", {}).get("State", "")),
                ("addr", n.get("Status", {}).get("Addr", "")),
            ), 1))
        self.series = {
            "skswarm_service_replicas_desired": desired,
            "skswarm_service_replicas_running": running,
            "skswarm_task_state_age_seconds": ages,
            "skswarm_service_task_failures_total": fails,
            "skswarm_node_info": info,
        }

    def render(self) -> str:
        """Exposition text of the current state."""
        out: list[str] = []

        def block(name: str, rows: list) -> None:
            kind, text = HELP[name]
            out.append(f"# HELP {name} {text}")
            out.append(f"# TYPE {name} {kind}")
            for pairs, v in rows:
                out.append(f"{name}{_labels(pairs)} {v if isinstance(v, int) else format(v, '.6f')}")

        for name in HELP:
            if name == "skswarm_api_duration_seconds":
                block(name, [((("endpoint", ep),), d) for ep, d in self.durations.items()])
            elif name == "skswarm_up":
                block(name, [((), self.up)])
            else:
                block(name, self.series.get(name, []))
        return "\n".join(out) + "\n"

    def render_after_poll(self) -> str:
        """poll() then render()."""
        self.poll()
        return self.render()


def make_api(host: str, timeout: float) -> Api:
    """An Api that GETs <host><path> with urllib and times the call."""
    def api(path: str) -> "tuple[Any, float]":
        t0 = time.monotonic()
        with urllib.request.urlopen(host.rstrip("/") + path, timeout=timeout) as r:
            body = json.loads(r.read())
        return body, time.monotonic() - t0
    return api


def make_server(get_body: Callable[[], str], port: int) -> ThreadingHTTPServer:
    """HTTP server: only GET /metrics answers 200, with the last render."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/metrics":
                body = get_body().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            else:
                body = b"not found\n"
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    return ThreadingHTTPServer(("", port), Handler)


def parse_env(env: Mapping[str, str]) -> dict:
    """Settings from SKSWARM_* environment variables, with defaults."""
    return {
        "host": env.get("SKSWARM_DOCKER_HOST", "http://socket-proxy:2375"),
        "poll_s": float(env.get("SKSWARM_POLL_S", "15")),
        "port": int(env.get("SKSWARM_PORT", "9323")),
        "timeout_s": float(env.get("SKSWARM_TIMEOUT_S", "10")),
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = parse_env(os.environ)
    exp = Exporter(make_api(cfg["host"], cfg["timeout_s"]), time.time)
    lock = threading.Lock()
    state = {"body": exp.render()}

    def loop() -> None:
        while True:
            body = exp.render_after_poll()
            with lock:
                state["body"] = body
            time.sleep(cfg["poll_s"])

    def get_body() -> str:
        with lock:
            return state["body"]

    threading.Thread(target=loop, daemon=True).start()
    make_server(get_body, cfg["port"]).serve_forever()


if __name__ == "__main__":
    main()
