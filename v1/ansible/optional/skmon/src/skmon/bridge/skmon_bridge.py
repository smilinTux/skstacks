#!/usr/bin/env python3
"""skmon-bridge: turn Alertmanager webhooks into calls of the estate's own sk-alert.

Routes on the main port: POST /alert, POST /watchdog, GET /metrics.
Route on the healthz port only: GET /healthz (the dead-man's switch probe).

One sk-alert call per webhook notification (one per Alertmanager group). A non-zero
exit or a timeout of sk-alert returns 500 so Alertmanager retries. Message bodies,
summaries, runbooks, the environment and the secret files are never logged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("skmon_bridge")

MAX_BODY_BYTES = 1024 * 1024
WATCHDOG_MAX_AGE_S = 180
TRUNCATED = " (truncated)"
TARGET_KEYS = ("node", "instance", "service", "job")


@dataclass
class Config:
    estate: str
    grafana_url: str
    skalert: str
    talk: bool
    alertmanager_url: str
    timeout_s: float = 20.0
    body_max: int = 1500
    config_dir: str | None = None  # SKALERT_CONFIG_DIR passed to sk-alert
    state_dir: str | None = None  # SKALERT_STATE_DIR passed to sk-alert


@dataclass
class State:
    last_watchdog: float | None = None
    last_delivery_ok: bool = True
    deliveries: int = 0
    failures: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


def level_for(severity: str) -> str:
    return {"critical": "crit", "warning": "warn"}.get(severity, "info")


def thread_key(estate: str, group_key: str) -> str:
    return f"skmon/{estate}/{hashlib.sha256(group_key.encode()).hexdigest()[:16]}"


def target_of(labels: dict) -> str:
    for key in (*TARGET_KEYS, "estate"):
        if labels.get(key):
            return str(labels[key])
    return ""


def _parse_time(value) -> datetime | None:
    """Parse an RFC 3339 time (nanoseconds allowed); None when missing, unparseable or the zero time."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    head, sep, tail = text.partition(".")
    if sep:
        digits = ""
        for i, ch in enumerate(tail):
            if not ch.isdigit():
                break
            digits += ch
        rest = tail[len(digits):]
        text = f"{head}.{digits[:6].ljust(6, '0')}{rest}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return None if parsed.year < 2 else parsed  # Alertmanager's unset time is 0001-01-01


def _human(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60}m"


def _duration(alerts: list) -> str | None:
    starts = [t for t in (_parse_time(a.get("startsAt")) for a in alerts) if t]
    ends = [t for t in (_parse_time(a.get("endsAt")) for a in alerts) if t]
    if not starts or not ends:
        return None
    seconds = int((max(ends) - min(starts)).total_seconds())
    return _human(seconds) if seconds >= 0 else None


def _group(payload: dict) -> tuple[dict, dict, list]:
    """(labels, annotations, alerts): the group's common values, else the first alert's."""
    alerts = [a for a in payload.get("alerts") or [] if isinstance(a, dict)]
    first = alerts[0] if alerts else {}
    labels = payload.get("commonLabels") or first.get("labels") or {}
    annotations = payload.get("commonAnnotations") or first.get("annotations") or {}
    return labels, annotations, alerts


def _alertname(payload: dict) -> str:
    labels, _, alerts = _group(payload)
    first = (alerts[0].get("labels") or {}) if alerts else {}
    return labels.get("alertname") or first.get("alertname") or "alert"


def build_call(payload: dict, cfg: Config, muted: int | None) -> list[str]:
    """argv for one sk-alert run. Does no I/O."""
    labels, annotations, alerts = _group(payload)
    first_labels = alerts[0].get("labels") or {} if alerts else {}
    alertname = _alertname(payload)
    level = level_for(labels.get("severity") or first_labels.get("severity") or "")
    argv = [cfg.skalert, "-l", level, "-g", thread_key(cfg.estate, str(payload.get("groupKey", "")))]
    if cfg.talk:
        argv.append("--talk")

    if payload.get("status") == "resolved":
        duration = _duration(alerts)
        argv += ["--resolve", "--", f"{alertname} resolved after {duration}" if duration else f"{alertname} resolved"]
        return argv

    target = target_of(labels) or target_of(first_labels)
    lines = []
    if annotations.get("summary"):
        lines.append(str(annotations["summary"]))
    if len(alerts) > 1:
        lines.append(f"({len(alerts)} alerts in this group)")
    if isinstance(muted, int) and not isinstance(muted, bool) and muted > 0:
        lines.append(f"{muted} dependent alerts muted.")
    if annotations.get("runbook"):
        lines.append(f"First check: {annotations['runbook']}")
    lines.append(f"Grafana: {cfg.grafana_url}/d/estate-overview")
    body = "\n".join(lines)
    if len(body) > cfg.body_max:
        body = body[: max(cfg.body_max - len(TRUNCATED), 0)] + TRUNCATED
    return argv + ["--title", f"{alertname} on {target}" if target else alertname, "--", body]


def _clean(text: str) -> str:
    """Make a string safe as an argv element: no NUL, no lone surrogates."""
    return text.replace("\x00", " ").encode("utf-8", "replace").decode("utf-8")


def muted_count(cfg: Config) -> int | None:
    """Alerts of this estate that Alertmanager currently inhibits; None on any error."""
    if not cfg.alertmanager_url:
        return None
    url = (f"{cfg.alertmanager_url.rstrip('/')}/api/v2/alerts"
           "?active=false&silenced=false&inhibited=true")
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 (operator-set URL)
            alerts = json.load(resp)
        return sum(1 for a in alerts if (a.get("labels") or {}).get("estate") == cfg.estate)
    except Exception:  # any failure just drops the muted line
        return None


def healthz(state: State, now: float) -> tuple[int, str]:
    with state.lock:
        last, ok = state.last_watchdog, state.last_delivery_ok
    if last is None:
        return 503, "no watchdog received yet"
    if now - last > WATCHDOG_MAX_AGE_S:
        return 503, "watchdog stale"
    if not ok:
        return 503, "last delivery failed"
    return 200, "ok"


def metrics_text(state: State) -> str:
    with state.lock:
        deliveries, failures, last = state.deliveries, state.failures, state.last_watchdog
    return (
        "# HELP skmon_bridge_deliveries_total Notifications delivered to sk-alert successfully.\n"
        "# TYPE skmon_bridge_deliveries_total counter\n"
        f"skmon_bridge_deliveries_total {deliveries}\n"
        "# HELP skmon_bridge_delivery_failures_total Notifications sk-alert failed to deliver (non-zero exit or timeout).\n"
        "# TYPE skmon_bridge_delivery_failures_total counter\n"
        f"skmon_bridge_delivery_failures_total {failures}\n"
        "# HELP skmon_bridge_last_watchdog_timestamp_seconds Unix time of the last Watchdog received (0 if none).\n"
        "# TYPE skmon_bridge_last_watchdog_timestamp_seconds gauge\n"
        f"skmon_bridge_last_watchdog_timestamp_seconds {last if last is not None else 0}\n"
    )


def _deliver(payload: dict, cfg: Config, state: State) -> bool:
    """Run sk-alert once for this notification; True on exit 0. Logs alertname, status, exit code only."""
    try:
        alertname = _clean(str(_alertname(payload)))
    except Exception:
        alertname = ""
    status = _clean(str(payload.get("status", "")))
    env = dict(os.environ)
    if cfg.config_dir:
        env["SKALERT_CONFIG_DIR"] = cfg.config_dir
    if cfg.state_dir:
        env["SKALERT_STATE_DIR"] = cfg.state_dir
    muted = None
    try:
        muted = muted_count(cfg) if status == "firing" else None
        argv = [_clean(a) for a in build_call(payload, cfg, muted)]
        proc = subprocess.run(argv, timeout=cfg.timeout_s, env=env, capture_output=True, check=False)
        code: object = proc.returncode
    except subprocess.TimeoutExpired:
        code = "timeout"
    except Exception as exc:  # a delivery that cannot even be attempted is a failed delivery
        code = f"error:{type(exc).__name__}"
    ok = code == 0
    with state.lock:
        state.last_delivery_ok = ok
        if ok:
            state.deliveries += 1
        else:
            state.failures += 1
    log.info("alertname=%s status=%s exit=%s muted=%s", alertname, status, code, muted)
    return ok


def _handler(cfg: Config, state: State, main: bool):
    routes = {"/alert": "POST", "/watchdog": "POST", "/metrics": "GET"} if main else {"/healthz": "GET"}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # request lines stay out of the logs
            pass

        def _send(self, code: int, text: str = "", ctype: str = "text/plain; charset=utf-8"):
            data = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method: str):
            path = urllib.parse.urlsplit(self.path).path
            want = routes.get(path)
            if want is None:
                return self._send(404, "not found\n")
            if want != method:
                return self._send(405, "method not allowed\n")
            if path == "/healthz":
                code, reason = healthz(state, time.time())
                return self._send(code, reason + "\n")
            if path == "/metrics":
                return self._send(200, metrics_text(state), "text/plain; version=0.0.4; charset=utf-8")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, "bad content-length\n")
            if length > MAX_BODY_BYTES:
                self.close_connection = True
                return self._send(413, "payload too large\n")
            try:
                payload = json.loads(self.rfile.read(length) or b"")
            except ValueError:
                return self._send(400, "invalid json\n")
            if not isinstance(payload, dict):
                return self._send(400, "invalid payload\n")
            if path == "/watchdog":
                with state.lock:
                    state.last_watchdog = time.time()
                return self._send(200, "ok\n")
            return self._send(200, "ok\n") if _deliver(payload, cfg, state) else self._send(500, "delivery failed\n")

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_DELETE(self):
            self._route("DELETE")

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True


def make_server(cfg: Config, state: State, host: str, port: int, healthz_port: int):
    return (_Server((host, port), _handler(cfg, state, True)),
            _Server((host, healthz_port), _handler(cfg, state, False)))


def config_from_env(env=os.environ) -> Config:
    return Config(
        estate=env.get("SKMON_ESTATE", ""),
        grafana_url=env.get("SKMON_GRAFANA_URL", "").rstrip("/"),
        skalert=env.get("SKMON_SKALERT", "/opt/sk-alert/sk-alert"),
        talk=env.get("SKMON_TALK", "0") == "1",
        alertmanager_url=env.get("SKMON_AM_URL", ""),
        config_dir=env.get("SKALERT_CONFIG_DIR") or None,
        state_dir=env.get("SKALERT_STATE_DIR", "/state"),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg, state = config_from_env(), State()
    main_srv, health_srv = make_server(cfg, state, "0.0.0.0", 8080, int(os.environ.get("SKMON_HEALTHZ_PORT", "9095")))

    def stop(*_):  # shutdown() blocks until serve_forever returns, so never call it inline
        for srv in (main_srv, health_srv):
            threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=health_srv.serve_forever, daemon=True).start()
    log.info("skmon-bridge up estate=%s", cfg.estate)
    main_srv.serve_forever()
    main_srv.server_close()
    health_srv.server_close()


if __name__ == "__main__":
    main()
