"""Shared helpers for the skhealth tests (not a test module itself).

* ``load_engine()`` imports the real engine script (src/skhealth/skhealth).
* ``FakeHost`` gives the engine a scratch world: a fake /proc, a state dir,
  a secrets file, and STUB binaries for every external command. Each stub
  records its full argv (one JSON line per call in calls.jsonl), so a test
  asserts on the exact arguments the engine passed, not only on what it
  did with the output. A stub answers from a rules file: the first rule
  whose argv prefix matches gives (rc, stdout).
* ``Hook`` is a local HTTP server that records every POST (path and body):
  the fake Telegram API and the fake webhook.
"""
from __future__ import annotations

import http.server
import importlib.machinery
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path

TESTS = Path(__file__).resolve().parent
APP = TESTS.parent / "ansible" / "optional" / "skhealth"
ENGINE = APP / "src" / "skhealth" / "skhealth"
WRAPPER = APP / "src" / "skhealth" / "skhealth-remote"

STUB = r'''#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(sys.argv[0]))
name = os.path.basename(sys.argv[0])
with open(os.path.join(here, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps([name] + sys.argv[1:]) + "\n")
rules = json.load(open(os.path.join(here, "rules.json")))
for rule in rules.get(name, []):
    pre = rule["argv"]
    if sys.argv[1:1 + len(pre)] == pre:
        sys.stdout.write(rule.get("out", ""))
        sys.exit(rule.get("rc", 0))
sys.exit(rules.get("_default_rc", {{}}).get(name, 0))
'''

NAMES = ("docker", "systemctl", "journalctl", "smartctl", "zpool", "mdadm", "ssh", "timeout", "stat", "openssl")


def load_engine():
    loader = importlib.machinery.SourceFileLoader("skhealth_engine", str(ENGINE))
    spec = importlib.util.spec_from_loader("skhealth_engine", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class FakeHost:
    def __init__(self, root: Path, names=NAMES):
        self.root = root
        self.bin = root / "bin"
        self.proc = root / "proc"
        self.state = root / "state"
        for d in (self.bin, self.proc / "pressure", self.state):
            d.mkdir(parents=True, exist_ok=True)
        self.rules: dict = {}
        for n in names:
            p = self.bin / n
            p.write_text(STUB.replace("{python}", sys.executable).replace("{{}}", "{}"))
            p.chmod(0o755)
        (self.bin / "calls.jsonl").write_text("")
        self.write_rules()
        (self.proc / "mounts").write_text("/dev/vda1 / ext4 rw 0 0\n")
        (self.proc / "loadavg").write_text("0.10 0.20 0.30 1/100 1234\n")
        (self.proc / "meminfo").write_text("MemTotal: 1000000 kB\nMemAvailable: 600000 kB\n")
        (self.proc / "pressure" / "memory").write_text(
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        self.secrets = root / "secrets.env"
        self.secrets.write_text("")
        self.config_path = root / "skhealth.json"
        self.cfg = {
            "cluster": "lab", "host_label": "node-a", "primary": "node-a",
            "confirm_runs": 2, "realert_hours": 6,
            "state_dir": str(self.state), "secrets_file": str(self.secrets),
            "proc_root": str(self.proc), "cpus": 2,
            "bin": {n: str(self.bin / n) for n in names},
            "checks": {
                "swarm": {"enabled": False}, "nodes": {"enabled": False},
                "host": {"enabled": True, "fs_paths": [], "nfs": False, "oom": False, "failed_units": True,
                         "load_per_cpu_max": 2.0, "mem_available_min_pct": 10, "mem_psi_full_avg60_max": 10},
                "disks": {"enabled": False}, "raid": {"enabled": False}, "tls": {"hosts": []},
            },
            "remote": {"hosts": []},
        }
        self.save()

    # rules: {binary: [{"argv": [...prefix], "rc": 0, "out": "..."}]}
    def on(self, name: str, argv: list[str], out: str = "", rc: int = 0) -> "FakeHost":
        self.rules.setdefault(name, []).append({"argv": argv, "out": out, "rc": rc})
        self.write_rules()
        return self

    def clear(self, name: str) -> "FakeHost":
        self.rules.pop(name, None)
        self.write_rules()
        return self

    def write_rules(self) -> None:
        (self.bin / "rules.json").write_text(json.dumps(self.rules))

    def save(self) -> None:
        self.config_path.write_text(json.dumps(self.cfg))

    def calls(self, name: str | None = None) -> list[list[str]]:
        out = [json.loads(l) for l in (self.bin / "calls.jsonl").read_text().splitlines() if l]
        return [c for c in out if name is None or c[0] == name]

    def reset_calls(self) -> None:
        (self.bin / "calls.jsonl").write_text("")


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.posts.append({"path": self.path, "body": body.decode()})
        code = self.server.status
        self.send_response(code)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class Hook:
    """Records POSTs; `status` sets the HTTP status it answers with."""

    def __init__(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.posts = []
        self.server.status = 200
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def posts(self) -> list[dict]:
        return self.server.posts

    def set_status(self, code: int) -> None:
        self.server.status = code

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def env_without_path() -> dict:
    """What a systemd timer gives a unit: no interactive PATH."""
    return {k: v for k, v in os.environ.items() if k != "PATH"} | {"PATH": "/usr/bin:/bin"}
