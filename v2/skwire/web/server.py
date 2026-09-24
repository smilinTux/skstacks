"""
skwire web server — a zero-dep (stdlib http.server) chat window over the engine.

`api_*` are pure functions (testable without a socket); the HTTP handler just wraps
them as JSON and serves the single-file chat UI (index.html). No frameworks, no
deps — cross-platform, runs anywhere Python does.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import (
    probe_env, suggest, build_plan, explain, approve, all_nodes,
    active_branding, load_packs_from_entrypoints, execute, offer_rotation_schedule,
    list_offerings, install_offering, load_catalog, recommend, redundancy_advice, list_packs,
    questions_for, get_pack, next_steps, interpret, converse, resolve_client,
)


def _html_bytes() -> bytes:
    """Read the chat UI — via importlib.resources so it works installed, in a wheel,
    AND inside a single-file zipapp (.pyz), falling back to the source tree in dev."""
    try:
        from importlib import resources
        return (resources.files("skwire.web") / "index.html").read_bytes()
    except Exception:
        return (Path(__file__).resolve().parent / "index.html").read_bytes()


# ── pure API (no I/O) ─────────────────────────────────────────────────────────
def api_branding() -> dict:
    b = active_branding()
    return {"name": b.name, "tagline": b.tagline, "logo": b.logo, "url": b.url, "accent": b.accent}


def api_scan() -> dict:
    p = probe_env()
    return {
        "env": {"os": p.os, "cpu_cores": p.cpu_cores, "ram_gb": p.ram_gb,
                "disk_free_gb": p.disk_free_gb, "gpus": p.gpus, "has_docker": p.has_docker,
                "has_kubectl": p.has_kubectl, "has_ollama": p.has_ollama},
        "suggestions": [{"kind": s.kind, "text": s.text} for s in suggest(p)],
    }


def api_plan() -> dict:
    nodes = all_nodes()
    if not nodes:
        return {"services": 0, "order": [], "text":
                "No packs registered yet — install or register one, then I'll wire it up.",
                "plan_hash": "", "mints": [], "edges": []}
    plan = build_plan(nodes)
    return {
        "services": len(plan.order), "order": list(plan.order), "text": explain(plan),
        "plan_hash": plan.plan_hash, "mints": sorted(plan.mints),
        "edges": [{"consumer": e.consumer, "provider": e.provider, "secret": e.secret} for e in plan.edges],
        "redundancy": [a.text for a in redundancy_advice(plan, nodes=nodes)],
    }


def api_next() -> dict:
    """A few recommended next actions, with exactly one flagged as the default."""
    return {"steps": [{"label": s.label, "command": s.command, "default": s.default}
                      for s in next_steps()]}


def _hf_search(query: str, kind: str = "models", limit: int = 5) -> list:
    """Live Hugging Face Hub search (stdlib urllib, no deps). Empty list if offline."""
    import urllib.parse, urllib.request
    path = "datasets" if kind.startswith("data") else "models"
    url = f"https://huggingface.co/api/{path}?search={urllib.parse.quote(query)}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=6) as r:          # nosec - read-only public API
            data = json.loads(r.read().decode())
        return [{"id": d.get("id"), "downloads": d.get("downloads", 0),
                 "likes": d.get("likes", 0)} for d in data][:limit]
    except Exception:
        return []


def _apply_action(kind: str, target: str, message: str) -> dict:
    """Execute a resolved action (shared by the LLM brain and the keyword floor)."""
    out: dict = {}
    if kind == "add_all":
        out["added"] = [o.name for o in list_offerings()
                        if install_offering(o.name).get("installed")]
        out["plan"] = api_plan()
    elif kind == "add_one" and target:
        res = install_offering(target)
        ok = bool(res.get("installed"))
        out["added"] = [target] if ok else []
        out["questions"] = questions_for(get_pack(target)) if ok else []
        out["plan"] = api_plan()
        if not ok:
            out["error"] = res.get("reason") or res.get("hint", "?")
    elif kind == "search" and target:
        out["results"] = _hf_search(
            target, kind="datasets" if "dataset" in message.lower() else "models")
    elif kind in ("plan", "approve"):
        out["plan"] = api_plan()
    return out


def api_chat(message: str, history=None) -> dict:
    """The conversational brain — a reliability/fluency hybrid.

    The deterministic router (interpret) owns the ACTION whenever it's confident,
    because tiny local models route a fixed catalog unreliably (gemma3:270m happily
    said "HF downloader" for "watch movies"). The model owns the REPLY, but only when
    it AGREES with the router — so the friendly wording never contradicts what we do.
    For turns the router can't classify (greetings, vague asks), we defer to the model
    if one is available. No model → the router handles everything."""
    load_catalog()
    history = history or []
    offs = list_offerings()
    det = interpret(message)
    valid_targets = {o.name for o in offs}

    llm = None
    client = resolve_client()
    if client is not None:
        try:
            r = converse(history, message, client, offerings=offs)
            llm = r if r.ok else None
        except Exception:
            llm = None

    def _emit(kind, target, reply, model, hist):
        out = {"kind": kind, "reply": reply, "model": model, "history": hist,
               "steps": api_next()["steps"]}
        if kind == "unknown":
            out["suggestions"] = det.suggestions
        out.update(_apply_action(kind, target, message))
        return out

    if det.kind != "unknown":
        # reliable routing. Use the model's nicer reply ONLY if it agrees on the target.
        agrees = (llm is not None and llm.action.get("kind") == det.kind
                  and llm.action.get("target", "") == (det.target or ""))
        if agrees:
            return _emit(det.kind, det.target, llm.reply, client.name, llm.history)
        reply = det.reply
        hist = history + [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
        return _emit(det.kind, det.target, reply, "keyword-router", hist)

    # router unsure → let the model converse if we have one. But the router already
    # found NO app match, so a model add_one here is a guess — don't install on it
    # (e.g. "hello"); only honor non-install intents. Keep the model's friendly reply.
    if llm is not None:
        kind = llm.action.get("kind", "none")
        target = llm.action.get("target", "")
        if kind == "add_one":
            kind, target = "none", ""        # no confirmed match → just converse, don't act
        return _emit(kind, target, llm.reply, client.name, llm.history)

    hist = history + [{"role": "user", "content": message}, {"role": "assistant", "content": det.reply}]
    return _emit("unknown", det.target, det.reply, "keyword-router", hist)


def api_catalog() -> dict:
    load_catalog()
    return {"offerings": [o.to_dict() for o in list_offerings()]}


def api_recommend(text: str) -> dict:
    load_catalog()
    return {"offerings": [o.to_dict() for o in recommend(text or "")]}


def api_add(name: str) -> dict:
    load_catalog()
    res = install_offering(name)
    if res.get("installed"):
        try:
            res["questions"] = questions_for(get_pack(name))   # the module's auto-generated flow
        except Exception:
            res["questions"] = []
    return res


def api_approve(plan_hash: str, approver: str = "user") -> dict:
    nodes = all_nodes()
    if not nodes:
        return {"approved": False, "reason": "nothing to approve"}
    plan = build_plan(nodes)
    if plan.plan_hash != plan_hash:
        return {"approved": False, "reason": "the plan changed since you saw it — re-plan first"}
    token = approve(plan, approver)
    res = execute(plan, token)               # mint → inject → wire (dry-run injector by default)
    offer = offer_rotation_schedule(nodes)
    wired = sum(1 for r in res.injected if r["ok"])
    svcs = list(plan.order)
    if not res.ok:
        note = f"Hit a snag wiring it: {res.error or 'see report'}."
    elif res.minted or wired:
        note = (f"Done 🎉 set up {len(svcs)} service(s) — minted {len(res.minted)} secret(s) "
                f"and wired {wired} connection(s).")
    else:
        # standalone tool(s) with nothing to wire — don't make success look empty
        note = (f"Done 🎉 {', '.join(svcs)} ready — no secrets to wire, it's standalone."
                if svcs else "Done 🎉")
    # collect any post-install guidance from the installed packs
    tips = []
    for name in list_packs():
        try:
            p = get_pack(name)
            if getattr(p, "post_install", "") and any(n["name"] in svcs for n in p.nodes):
                tips.append(p.post_install)
        except Exception:
            pass
    return {"approved": True, "plan_hash": token.plan_hash, "approver": token.approver,
            "executed": res.ok, "minted": list(res.minted), "wired": wired, "note": note,
            "tips": tips, "rotation_offer": offer.message if offer.schedule else ""}


# ── HTTP wrapper ──────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def _send(self, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(_html_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/branding":
            return self._send(api_branding())
        if self.path == "/api/scan":
            return self._send(api_scan())
        if self.path == "/api/plan":
            return self._send(api_plan())
        if self.path == "/api/catalog":
            return self._send(api_catalog())
        if self.path == "/api/next":
            return self._send(api_next())
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if self.path == "/api/approve":
            return self._send(api_approve(data.get("plan_hash", ""), data.get("approver", "user")))
        if self.path == "/api/recommend":
            return self._send(api_recommend(data.get("text", "")))
        if self.path == "/api/chat":
            return self._send(api_chat(data.get("message", ""), data.get("history")))
        if self.path == "/api/add":
            return self._send(api_add(data.get("name", "")))
        self.send_error(404)

    def log_message(self, *a):  # quiet
        pass


def serve(port: int = 8773, open_browser: bool = True) -> None:
    load_packs_from_entrypoints()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"{active_branding().name} chat window → {url}  (Ctrl-C to stop)")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
