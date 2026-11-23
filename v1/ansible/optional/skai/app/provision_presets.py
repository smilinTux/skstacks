"""Reconcile skai's corpus access in Open WebUI through its own API.

Runs as the ``provision`` service of the skai stack (a loop), and can run
once by hand inside the skai container:

    python3 /provision/provision_presets.py --loop 300 /provision/presets.json
    python3 /provision/provision_presets.py /provision/presets.json      # once

It signs in as the provisioner admin that Open WebUI creates from
``WEBUI_ADMIN_EMAIL`` / ``WEBUI_ADMIN_PASSWORD`` on first start (both in the
environment, never on a command line), then, from the JSON file:

1. **Groups**: every group a corpus is allowed to (``allowed_groups``) must
   exist in Open WebUI under the same name as the Authentik group; a missing
   one is created empty. With OIDC group management on, Open WebUI fills
   them from the groups claim at every login, so Authentik stays the source
   of membership. ``*`` means every signed-in user and needs no group.
2. **Tool servers**: Open WebUI's access grants name group ids, which only
   exist at runtime, so the env (``TOOL_SERVER_CONNECTIONS``) registers each
   corpus's tool server with no grants (admin only, fail closed) and this
   step sets each one's read grants to its corpus's groups. Open WebUI runs
   with ``ENABLE_PERSISTENT_CONFIG=false``, so a restart puts the env value
   back; the loop restores the grants within one interval.
3. **Presets**: ``GET /api/v1/models/model?id=...``; absent (404),
   ``POST /api/v1/models/create``; present and different,
   ``POST /api/v1/models/model/update``; grants from the same groups.

Nothing is ever deleted, and a group, connection or model that is not in
the file is never touched. Python standard library only.

Prints ``GROUP_CREATED <name>``, ``TOOLS_UPDATED <ids>`` / ``TOOLS_UNCHANGED``,
``PRESET_CREATED|UPDATED|UNCHANGED <id>``, then ``RECONCILED`` after a good
pass or ``RECONCILE_ERROR <reason>``. Once: exit 1 on error. Loop: retry.

File format: ``{"presets": [...], "tool_servers": {"<info.id>": [groups]}}``;
a bare list is read as presets only (no tool server step).
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
ANYONE = "*"


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


def grants(names: list[str], group_ids: dict[str, str]) -> list[dict]:
    """Read grants for the named groups; ``*`` = every signed-in user. A
    name without a group id grants nothing (fail closed)."""
    out = []
    for name in names:
        if name == ANYONE:
            out.append({"principal_type": "user", "principal_id": "*", "permission": "read"})
        elif name in group_ids:
            out.append({"principal_type": "group", "principal_id": group_ids[name], "permission": "read"})
    return out


def ensure_groups(names: list[str], token: str, opener=urllib.request.urlopen) -> tuple[dict[str, str], list[str]]:
    wanted = sorted({n for n in names if n != ANYONE})
    if not wanted:
        return {}, []
    status, data = call("GET", "/api/v1/groups/", token, opener=opener)
    if status != 200 or not isinstance(data, list):
        raise ApiError(f"listing groups failed (HTTP {status})")
    ids = {g["name"]: g["id"] for g in data if isinstance(g, dict) and g.get("name") and g.get("id")}
    out = []
    for name in wanted:
        if name in ids:
            continue
        status, g = call("POST", "/api/v1/groups/create", token,
                         {"name": name, "description": "managed by skstacks skai (members from the SSO groups claim)"},
                         opener=opener)
        if status != 200 or not isinstance(g, dict) or not g.get("id"):
            raise ApiError(f"creating group {name} failed (HTTP {status})")
        ids[name] = g["id"]
        out.append(f"GROUP_CREATED {name}")
    return ids, out


def _grant_set(gs: list[dict] | None) -> set:
    return {(g.get("principal_type"), g.get("principal_id"), g.get("permission")) for g in gs or []}


def reconcile_tools(wanted: dict[str, list[str]], group_ids: dict[str, str], token: str,
                    opener=urllib.request.urlopen) -> list[str]:
    """Set the read grants of the tool servers named in ``wanted`` (by
    info.id); every other connection, and every other field, stays as is."""
    if not wanted:
        return []
    status, data = call("GET", "/api/v1/configs/tool_servers", token, opener=opener)
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("TOOL_SERVER_CONNECTIONS"), list):
        raise ApiError(f"reading tool servers failed (HTTP {status})")
    conns = data["TOOL_SERVER_CONNECTIONS"]
    seen, changed = set(), []
    for conn in conns:
        sid = (conn.get("info") or {}).get("id")
        if sid not in wanted:
            continue
        seen.add(sid)
        want = grants(wanted[sid], group_ids)
        cfg = dict(conn.get("config") or {})
        if _grant_set(cfg.get("access_grants")) != _grant_set(want):
            cfg["access_grants"] = want
            conn["config"] = cfg
            changed.append(sid)
    missing = sorted(set(wanted) - seen)
    if missing:
        raise ApiError(f"tool servers not registered in Open WebUI (TOOL_SERVER_CONNECTIONS): {', '.join(missing)}")
    if not changed:
        return ["TOOLS_UNCHANGED"]
    status, _ = call("POST", "/api/v1/configs/tool_servers", token, {"TOOL_SERVER_CONNECTIONS": conns}, opener=opener)
    if status != 200:
        raise ApiError(f"writing tool servers failed (HTTP {status})")
    return [f"TOOLS_UPDATED {','.join(changed)}"]


def form(preset: dict, group_ids: dict[str, str] | None = None) -> dict:
    """The ModelForm Open WebUI expects, from one rendered preset. A preset
    with ``allowed_groups`` gets grants for those groups; otherwise its own
    ``access_grants`` (default none: admin only)."""
    if "allowed_groups" in preset:
        access = grants(preset["allowed_groups"], group_ids or {})
    else:
        access = preset.get("access_grants", [])
    return {
        "id": preset["id"],
        "base_model_id": preset["base_model_id"],
        "name": preset["name"],
        "meta": preset["meta"],
        "params": preset["params"],
        "access_grants": access,
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


def provision(presets: list[dict], token: str, opener=urllib.request.urlopen,
              group_ids: dict[str, str] | None = None) -> list[str]:
    out = []
    for preset in presets:
        wanted = form(preset, group_ids)
        q = urllib.parse.urlencode({"id": wanted["id"]})
        status, current = call("GET", f"/api/v1/models/model?{q}", token, opener=opener)
        if status == 200 and isinstance(current, dict) and current.get("id") == wanted["id"]:
            if same(current, wanted):
                out.append(f"PRESET_UNCHANGED {wanted['id']}")
                continue
            status, _ = call("POST", "/api/v1/models/model/update", token, wanted, opener=opener)
            verb = "UPDATED"
        elif status == 404:  # v0.13: 404 = no such model; 401 = it exists but we may not read it
            status, _ = call("POST", "/api/v1/models/create", token, wanted, opener=opener)
            verb = "CREATED"
        else:
            raise ApiError(f"reading preset {wanted['id']} failed (HTTP {status})")
        if status != 200:
            raise ApiError(f"writing preset {wanted['id']} failed (HTTP {status})")
        out.append(f"PRESET_{verb} {wanted['id']}")
    return out


def load(path: str) -> tuple[list[dict], dict[str, list[str]]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data, {}
    return list(data.get("presets") or []), dict(data.get("tool_servers") or {})


def reconcile(presets: list[dict], tools: dict[str, list[str]], opener=urllib.request.urlopen) -> list[str]:
    token = signin(opener)
    names = [g for p in presets for g in p.get("allowed_groups", [])] + [g for gs in tools.values() for g in gs]
    group_ids, out = ensure_groups(names, token, opener)
    out += reconcile_tools(tools, group_ids, token, opener)
    out += provision(presets, token, opener, group_ids)
    return out


def main(argv: list[str]) -> int:
    args = argv[1:]
    interval = 0
    if len(args) == 3 and args[0] == "--loop" and args[1].isdigit():
        interval = max(30, int(args[1]))
        args = args[2:]
    if len(args) != 1:
        print("usage: provision_presets.py [--loop SECONDS] <presets.json>", file=sys.stderr)
        return 2
    while True:
        try:
            presets, tools = load(args[0])
            if not presets and not tools:
                print("no presets configured", flush=True)
                if not interval:
                    return 0
            else:
                wait_ready()
                for line in reconcile(presets, tools):
                    print(line, flush=True)
                print("RECONCILED", flush=True)
        except (ApiError, OSError, ValueError) as exc:
            print(f"RECONCILE_ERROR {exc}", file=sys.stderr, flush=True)
            if not interval:
                return 1
        if not interval:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
