"""Create or update skai's corpus model presets through Open WebUI's own API.

Run by the deploy inside the skai container (``docker exec``), so it talks to
http://127.0.0.1:8080 and needs no network path from outside:

    python3 /provision/provision_presets.py /provision/presets.json

It signs in as the provisioner admin that Open WebUI creates from
``WEBUI_ADMIN_EMAIL`` / ``WEBUI_ADMIN_PASSWORD`` on first start (both are in
the container's environment, so no secret is on a command line), then for
each preset in the JSON file: ``GET /api/v1/models/model?id=...``; absent,
``POST /api/v1/models/create``; present, ``POST /api/v1/models/model/update``.
A preset is never deleted, and a model id that is not in the file is never
touched. Python standard library only (it runs on the app image's Python).

Prints one line per preset: ``PRESET_CREATED <id>``, ``PRESET_UPDATED <id>``
or ``PRESET_UNCHANGED <id>``; exits non-zero on the first error.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("SKAI_PROVISION_URL", "http://127.0.0.1:8080")
TIMEOUT = 30


class ApiError(RuntimeError):
    pass


def call(method: str, path: str, token: str | None = None, body: dict | None = None,
         opener=urllib.request.urlopen) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except ValueError:
            return e.code, raw.decode(errors="replace")[:200]


def wait_ready(opener=urllib.request.urlopen, tries: int = 60, delay: float = 5.0) -> None:
    for _ in range(tries):
        try:
            status, _ = call("GET", "/health", opener=opener)
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(delay)
    raise ApiError("Open WebUI did not answer /health")


def signin(opener=urllib.request.urlopen) -> str:
    email = os.environ.get("WEBUI_ADMIN_EMAIL", "")
    password = os.environ.get("WEBUI_ADMIN_PASSWORD", "")
    if not email or not password:
        raise ApiError("WEBUI_ADMIN_EMAIL and WEBUI_ADMIN_PASSWORD must be set in the container")
    status, data = call("POST", "/api/v1/auths/signin", body={"email": email, "password": password}, opener=opener)
    if status != 200 or not isinstance(data, dict) or not data.get("token"):
        raise ApiError(f"provisioner sign-in failed (HTTP {status})")
    if data.get("role") != "admin":
        raise ApiError("the provisioner account is not an admin (was another user created first?)")
    return data["token"]


def form(preset: dict) -> dict:
    """The ModelForm Open WebUI expects, from one rendered preset."""
    return {
        "id": preset["id"],
        "base_model_id": preset["base_model_id"],
        "name": preset["name"],
        "meta": preset["meta"],
        "params": preset["params"],
        "access_grants": preset.get("access_grants", []),
        "is_active": True,
    }


def same(current: dict, wanted: dict) -> bool:
    if current.get("base_model_id") != wanted["base_model_id"] or current.get("name") != wanted["name"]:
        return False
    if (current.get("params") or {}) != wanted["params"]:
        return False
    cur_meta = current.get("meta") or {}
    if any(cur_meta.get(k) != v for k, v in wanted["meta"].items()):
        return False
    want_grants = {(g["principal_type"], g["principal_id"], g["permission"]) for g in wanted["access_grants"]}
    have_grants = {(g.get("principal_type"), g.get("principal_id"), g.get("permission"))
                   for g in current.get("access_grants") or []}
    return want_grants == have_grants and current.get("is_active", True)


def provision(presets: list[dict], token: str, opener=urllib.request.urlopen) -> list[str]:
    out = []
    for preset in presets:
        wanted = form(preset)
        q = urllib.parse.urlencode({"id": wanted["id"]})
        status, current = call("GET", f"/api/v1/models/model?{q}", token, opener=opener)
        if status == 200 and isinstance(current, dict) and current.get("id") == wanted["id"]:
            if same(current, wanted):
                out.append(f"PRESET_UNCHANGED {wanted['id']}")
                continue
            status, _ = call("POST", "/api/v1/models/model/update", token, wanted, opener=opener)
            verb = "UPDATED"
        elif status in (200, 401, 404) and not (isinstance(current, dict) and current.get("id")):
            status, _ = call("POST", "/api/v1/models/create", token, wanted, opener=opener)
            verb = "CREATED"
        else:
            raise ApiError(f"reading preset {wanted['id']} failed (HTTP {status})")
        if status != 200:
            raise ApiError(f"writing preset {wanted['id']} failed (HTTP {status})")
        out.append(f"PRESET_{verb} {wanted['id']}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: provision_presets.py <presets.json>", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        presets = json.load(fh)
    if not presets:
        print("no presets configured")
        return 0
    try:
        wait_ready()
        token = signin()
        for line in provision(presets, token):
            print(line)
    except (ApiError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
