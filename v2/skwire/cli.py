"""
skwire CLI — the `skwire` command the all-in-one installer wires up.

    skwire scan              # probe the env + tailored suggestions ("here or elsewhere?")
    skwire packs             # list discovered/registered packs
    skwire plan [--pack X]   # build + narrate the wiring plan ("…want me to just do it?")

`up`/`configure` (execute) land with the mint→inject→wire executor. Today the CLI
covers scan + plan + narrate (the conversational front half).
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional


def _force_utf8() -> None:
    """skwire prints emoji/✓/⭐. On Windows the default console is cp1252 and those
    crash with UnicodeEncodeError — reconfigure stdio to UTF-8 (errors='replace' so
    it degrades instead of dying). No-op where stdout is already UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

from . import (
    probe_env, suggest, build_plan, explain, all_nodes, list_packs,
    load_packs_from_entrypoints, approve, execute, rotate, offer_rotation_schedule,
    RandomSecretStore, RecordingInjector,
    load_catalog, list_offerings, install_offering, recommend, redundancy_advice, doctor,
    questions_for, get_pack, next_step, next_steps, active_branding,
)


def _model(action: str) -> int:
    """Show the resolved model, or provision the tiny default on a fresh box."""
    from .llm import resolve_client, SMALL_MODEL
    if action == "setup":
        from .model import ensure_model
        print(f"Setting up a small chat model ({SMALL_MODEL}) so {active_branding().name} can talk to you…")
        s = ensure_model()
        print(f"  provider: {s.provider}  model: {s.model}")
        print(f"  {'✓' if s.ready else '→'} {s.detail}")
        return 0 if s.ready else 1
    client = resolve_client()
    if client is None:
        print("No chat model available — running in basic (keyword) mode.")
        print(f"Run `{active_branding().name.lower()} model setup` to grab the tiny default ({SMALL_MODEL}).")
        return 0
    print(f"Chat model: {client.name}")
    return 0


def _print_next() -> None:
    """Close every command with a few options — the default marked with ⭐."""
    steps = next_steps()
    print("\n👉 What next?")
    for s in steps:
        mark = " ⭐ (recommended)" if s.default else ""
        print(f"   • {s.label}: `{s.command}`{mark}")


def _scan() -> int:
    p = probe_env()
    print(
        f"OS={p.os} cores={p.cpu_cores} ram={p.ram_gb:g}GB "
        f"disk_free={p.disk_free_gb:g}GB docker={p.has_docker} k8s={p.has_kubectl} "
        f"ollama={p.has_ollama} gpu={p.gpus or 'none'}"
    )
    print("Mind if I take it from here?")
    for s in suggest(p):
        print(("  ? " if s.kind == "question" else "  * ") + s.text)
    return 0


def _packs() -> int:
    names = list_packs()
    print("\n".join(names) if names else
          "(no packs registered — `pip install` a project's pack, or register one in code)")
    return 0


def _plan() -> int:
    nodes = all_nodes()
    if not nodes:
        print("No nodes to plan — install/register a pack first (`skwire packs`).")
        return 1
    plan = build_plan(nodes)
    print(explain(plan))
    for a in redundancy_advice(plan, nodes=nodes):
        print('  ' + a.text)
    return 0


def _up() -> int:
    nodes = all_nodes()
    if not nodes:
        print("No nodes — install/register a pack first (`skwire packs`)."); return 1
    plan = build_plan(nodes)
    print(explain(plan))
    for a in redundancy_advice(plan, nodes=nodes):
        print('  ' + a.text)
    if input("\n[heck yeah / n] > ").strip().lower() not in ("", "y", "yes", "heck yeah", "heck yeah!"):
        print("No worries — nothing changed."); return 0
    store, inj = RandomSecretStore(), RecordingInjector()
    res = execute(plan, approve(plan, "cli-user"), store=store, injector=inj)
    wired = sum(1 for r in res.injected if r["ok"])
    print(f"Done — minted {len(res.minted)} secret(s), wired {wired} connection(s)." if res.ok
          else f"Snag: {res.error}")
    offer = offer_rotation_schedule(nodes)
    if offer.schedule:
        print("🔑 " + offer.message)
    return 0 if res.ok else 1


def _rotate(keys) -> int:
    nodes = all_nodes()
    if not nodes:
        print("No nodes — nothing to rotate."); return 1
    plan = build_plan(nodes)
    res = rotate(plan, RandomSecretStore(), RecordingInjector(), keys=keys or None)
    print(f"Rotated {res.rotated} → re-injected across the vertical "
          f"({sum(1 for r in res.injected if r['ok'])} targets).")
    return 0 if res.ok else 1


def _catalog(query) -> int:
    load_catalog()
    offs = recommend(" ".join(query)) if query else list_offerings()
    print("Here's what I can set up for you (all best-of-breed open source):\n")
    for o in offs:
        print(f"  {o.icon} {o.name} — {o.title}")
        print(f"     {o.description}")
        if o.repo:
            print(f"     open source ({o.license}) · {o.repo} · fork it / contribute back")
    print("\nAdd one with:  skwire add <name>")
    return 0


def _add(name) -> int:
    load_catalog()
    try:
        res = install_offering(name)
    except KeyError:
        print(f"No offering named {name!r}. Try `skwire catalog`."); return 1
    if res.get("installed"):
        print(f"Added {name}. Run `skwire up` to wire it in.")
        try:
            qs = questions_for(get_pack(name))
            if qs:
                print("It'll ask you:")
                for q in qs: print("  ? " + q)
        except Exception:
            pass
    else:
        print(f"{name}: {res.get('hint', res.get('reason', 'see catalog'))}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(prog="skwire", description="The universal bootstrapper / wiring fabric.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("scan", help="probe the environment + suggestions")
    sub.add_parser("doctor", help="self-check env + packs + graph health")
    sub.add_parser("packs", help="list discovered/registered packs")
    sub.add_parser("plan", help="build + narrate the wiring plan from registered packs")
    sub.add_parser("up", help="plan → approve → mint → inject → wire (the whole thing)")
    rt = sub.add_parser("rotate", help="re-mint + re-inject key(s) across the vertical")
    rt.add_argument("keys", nargs="*", help="specific keys to rotate (default: all)")
    ct = sub.add_parser("catalog", help="browse available offerings (best-of-breed OSS)")
    ct.add_argument("query", nargs="*", help="optional: what you're after (AI picks)")
    ad = sub.add_parser("add", help="add an offering from the catalog")
    ad.add_argument("name", help="offering name (see `skwire catalog`)")
    sv = sub.add_parser("serve", help="open the slick chat-window UI (no terminal)")
    sv.add_argument("--port", type=int, default=8773)
    sv.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    md = sub.add_parser("model", help="set up / check the tiny chat model (no LLM? we get you one)")
    md.add_argument("action", nargs="?", default="status", choices=["status", "setup"])
    args = parser.parse_args(argv)

    # Auto-discover installed packs (entry-point group `skwire.packs`).
    try:
        load_packs_from_entrypoints()
    except Exception:
        pass  # discovery is best-effort; code-registered packs still work

    if args.cmd == "scan":
        rc = _scan(); _print_next(); return rc
    if args.cmd == "doctor":
        ok=True
        for c in doctor():
            print(("  ✓ " if c["ok"] else "  ✗ ")+c["name"]+": "+c["detail"]); ok=ok and c["ok"]
        _print_next(); return 0 if ok else 1
    if args.cmd == "packs":
        rc = _packs(); _print_next(); return rc
    if args.cmd == "plan":
        rc = _plan(); _print_next(); return rc
    if args.cmd == "up":
        return _up()
    if args.cmd == "rotate":
        return _rotate(args.keys)
    if args.cmd == "catalog":
        rc = _catalog(args.query); _print_next(); return rc
    if args.cmd == "add":
        rc = _add(args.name); _print_next(); return rc
    if args.cmd == "serve":
        from .web.server import serve
        serve(port=args.port, open_browser=not args.no_open)
        return 0
    if args.cmd == "model":
        return _model(args.action)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
