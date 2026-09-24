"""
skbloom web — the app-store / concierge UI over the installer.

Zero-dep stdlib http.server. `api_*` are pure functions (testable without a socket):
list the catalog, turn intent into a validated profile + the named-step plan, and (on
approval) run the flow. The single-file index.html is the slick surface.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..concierge import load_services, propose_profile, concierge_reply
from ..flow import install_flow, Profile
from ..steps import StateStore, iter_steps

_HTML = Path(__file__).resolve().parent / "index.html"


def _v2_root() -> str:
    return os.environ.get("SKBLOOM_V2", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


def _client():
    try:
        from skwire import resolve_client
        return resolve_client()
    except Exception:
        return None


def api_services() -> dict:
    svcs = load_services(_v2_root())
    return {"services": [{
        "name": s.name, "path": s.path, "capability": s.capability, "provider": s.provider,
        "ha": s.ha, "min_replicas": s.min_replicas,
        "config": s.config,                 # tunable knobs (key → default) for the tune panel
        "secrets": list(s.secrets),         # key names only (never values)
    } for s in svcs]}


def _noop_run(cmd, **kw):
    return ("", 0)


def api_propose(intent: str, cluster: str = "skbloom") -> dict:
    from ..rotation import rotation_plan
    svcs = load_services(_v2_root())
    profile = propose_profile(intent or "", svcs, cluster=cluster)
    reply = concierge_reply(intent or "", profile, svcs, client=_client())
    # the named-step plan (no side effects — a dry runner)
    steps = install_flow(profile, run=_noop_run, v2_root=_v2_root())
    plan = [s.name for s in steps] if profile.services else []
    # the auto-rotation story for this stack (creds + certs)
    rot = rotation_plan(_v2_root(), profile.services, tls=True)
    rotation = {"secrets": sum(1 for r in rot if r.kind == "secret"),
                "certs": sum(1 for r in rot if r.kind == "cert"),
                "all_automatic": all(r.automatic for r in rot)}
    return {"reply": reply, "profile": {"cluster": profile.cluster, "services": profile.services},
            "plan": plan, "rotation": rotation}


def _state_dir() -> str:
    return os.environ.get("SKBLOOM_STATE_DIR", os.path.expanduser("~/.skbloom"))


def api_status() -> dict:
    """Control-plane view: installed stacks (from the state files) + what they deployed."""
    import glob
    clusters = []
    for f in sorted(glob.glob(os.path.join(_state_dir(), "*.json"))):
        name = os.path.basename(f)[:-5]
        try:
            done = list(json.loads(open(f).read()).get("completed", []))
        except Exception:
            done = []
        services = [s.split(":", 1)[1] for s in done if s.startswith("deploy:")]
        meta = {}
        try:
            meta = json.loads(open(f).read()).get("meta", {})
        except Exception:
            pass
        urls = meta.get("urls", {})
        clusters.append({"cluster": name, "steps_done": len(done),
                         "services": [{"name": s, "url": urls.get(s)} for s in services],
                         "complete": "final-check" in done})
    return {"clusters": clusters}


def api_branding() -> dict:
    try:
        from skwire import active_branding
        b = active_branding()
        return {"name": b.name if b.name != "skwire" else "skbloom",
                "accent": b.accent, "logo": b.logo or "🌱"}
    except Exception:
        return {"name": "skbloom", "accent": "#22c55e", "logo": "🌱"}


def _system_runner(cmd, **kw):
    import subprocess
    p = subprocess.run(cmd, capture_output=True, text=True)   # nosec - argv list
    return (p.stdout, p.returncode)


# ── day-2 control plane ──────────────────────────────────────────────────────
# Live cluster ops (vs api_status which reads ~/.skbloom/*.json state files).
# Every api_* takes an injectable `run=` so tests drive a FakeRunner — no cluster.

def api_health(cluster: str, run=_system_runner) -> dict:
    """Live readiness per service-namespace from `kubectl get pods -A -o json`.
    Pods are grouped by their namespace (= service name); ready/total counts the
    container readiness gates. Fail-soft: returns {} on any error."""
    cmd = ["kubectl", "--context", f"k3d-{cluster}", "get", "pods", "-A", "-o", "json"]
    try:
        out, rc = run(cmd)
        if rc != 0:
            return {}
        items = json.loads(out).get("items", [])
    except Exception:
        return {}
    by_ns: dict = {}
    for pod in items:
        ns = pod.get("metadata", {}).get("namespace", "")
        statuses = pod.get("status", {}).get("containerStatuses", []) or []
        total = len(statuses)
        ready = sum(1 for cs in statuses if cs.get("ready"))
        agg = by_ns.setdefault(ns, {"ready": 0, "total": 0})
        agg["ready"] += ready
        agg["total"] += total
    services = [{"name": ns, "ready": v["ready"], "total": v["total"],
                 "healthy": v["total"] > 0 and v["ready"] == v["total"]}
                for ns, v in sorted(by_ns.items())]
    return {"services": services}


def api_restart(cluster: str, service: str, run=_system_runner) -> dict:
    """Roll-restart a service's deployment: `kubectl rollout restart deploy/<svc> -n <svc>`."""
    cmd = ["kubectl", "--context", f"k3d-{cluster}",
           "rollout", "restart", f"deploy/{service}", "-n", service]
    out, rc = run(cmd)
    return {"ok": rc == 0, "output": out}


def api_scale(cluster: str, service: str, replicas: int, run=_system_runner) -> dict:
    """Scale a service's deployment: `kubectl scale deploy/<svc> -n <svc> --replicas=N`."""
    cmd = ["kubectl", "--context", f"k3d-{cluster}",
           "scale", f"deploy/{service}", "-n", service, f"--replicas={int(replicas)}"]
    out, rc = run(cmd)
    return {"ok": rc == 0, "output": out}


def _log_lines(cluster: str, service: str, run=_system_runner):
    """Pure line generator for streaming logs — yields each log line. The runner
    returns (stdout, rc); we split stdout into lines so a fake runner can drive it
    (tests) while the live runner returns `kubectl logs` output."""
    cmd = ["kubectl", "--context", f"k3d-{cluster}",
           "logs", "-f", "--tail=100", f"deploy/{service}", "-n", service]
    try:
        out, rc = run(cmd)
    except Exception:
        return
    if rc != 0:
        return
    for line in (out or "").splitlines():
        yield line


class _Handler(BaseHTTPRequestHandler):
    def _send(self, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(_HTML.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/services":
            return self._send(api_services())
        if self.path == "/api/branding":
            return self._send(api_branding())
        if self.path == "/api/status":
            return self._send(api_status())
        if self.path.startswith("/api/health"):
            q = parse_qs(urlparse(self.path).query)
            return self._send(api_health(q.get("cluster", ["skbloom"])[0]))
        if self.path.startswith("/api/logs"):
            q = parse_qs(urlparse(self.path).query)
            return self._stream_logs(q.get("cluster", ["skbloom"])[0],
                                     q.get("service", [""])[0])
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if self.path == "/api/propose":
            return self._send(api_propose(data.get("intent", ""), data.get("cluster", "skbloom")))
        if self.path == "/api/up":
            return self._stream_up(data)
        if self.path == "/api/restart":
            return self._send(api_restart(data.get("cluster", "skbloom"), data.get("service", "")))
        if self.path == "/api/scale":
            return self._send(api_scale(data.get("cluster", "skbloom"),
                                        data.get("service", ""), int(data.get("replicas", 1))))
        self.send_error(404)

    def _stream_up(self, data):
        """Run the install, streaming each named-step event to the browser (SSE)."""
        prof = Profile(cluster=data.get("cluster", "skbloom"),
                       services=list(data.get("services") or []),
                       secret_seed=list(data.get("seed") or []),
                       tls=bool(data.get("tls")),
                       config_overrides=dict(data.get("config_overrides") or {}))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        from ..flow import service_urls
        store = StateStore(os.path.join(_state_dir(), f"{prof.cluster}.json"))
        # record the one-stop-shop URLs so the Stacks view links every service
        store.set_meta({"urls": service_urls(prof, _v2_root()), "domain": prof.domain, "tls": prof.tls})
        steps = install_flow(prof, run=_system_runner, v2_root=_v2_root())
        try:
            for ev in iter_steps(steps, store):
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_logs(self, cluster, service):
        """Stream a deployment's logs to the browser line-by-line (SSE)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for line in _log_lines(cluster, service, run=_system_runner):
                self.wfile.write(f"data: {json.dumps(line)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


def serve(port: int = 8774, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"skbloom app-store → {url}  (Ctrl-C to stop)")
    if open_browser:
        try:
            import webbrowser; webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
