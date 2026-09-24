"""
skbloom CLI — stand up a sovereign SKStacks stack from one command.

    skbloom plan      # show the named-step plan (no changes)
    skbloom up        # run the flow (resumable, idempotent) with live narration
    skbloom resume    # alias for up — picks up after a failed/interrupted run
    skbloom status    # what's done / what's left
    skbloom reset     # clear progress (does NOT tear down the cluster)

The AI concierge (gather intent → emit a validated profile) layers on top of this in a
later increment; the engine here is deterministic and the only thing that mutates state.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from .flow import Profile, install_flow
from .steps import StateStore, run_steps


def _force_utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def system_runner(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True)   # nosec - argv list
    return (p.stdout, p.returncode)


def _state_path():
    return os.environ.get("SKBLOOM_STATE", os.path.expanduser("~/.skbloom/steps.json"))


def _v2_root():
    return os.environ.get("SKBLOOM_V2", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _narrate(ev, step):
    icon = {"start": "▶", "done": "✓", "skipped": "·", "failed": "✗"}.get(ev, " ")
    if ev == "start":
        print(f"  {icon} {step.name} — {step.description}")
    elif ev in ("done", "skipped", "failed"):
        print(f"    {icon} {ev}")


def main(argv=None) -> int:
    _force_utf8()
    ap = argparse.ArgumentParser(prog="skbloom", description="AI-first sovereign SKStacks installer.")
    ap.add_argument("cmd", nargs="?", default="plan",
                    choices=["plan", "up", "resume", "status", "reset", "serve", "rotation"])
    ap.add_argument("intent", nargs="*", default=[], help="natural-language intent, e.g. 'sso and ingress'")
    ap.add_argument("--cluster", default="skbloom")
    ap.add_argument("--platform", choices=["k8s", "swarm"], default="k8s",
                    help="k8s (k3d) or a single-node docker swarm")
    ap.add_argument("--services", help="comma-separated descriptor paths (default: sovereign set)")
    ap.add_argument("--seed", action="append", default=[],
                    help="seed the fake secret store: key=value (repeatable; demo/test)")
    ap.add_argument("--tls", action="store_true", help="instant local HTTPS via a cert-manager CA")
    args = ap.parse_args(argv)

    prof = Profile(cluster=args.cluster, tls=args.tls, platform=args.platform)
    intent = " ".join(args.intent).strip()
    if intent:
        # concierge: natural-language intent → a VALIDATED profile (real services only)
        from .concierge import load_services, propose_profile, concierge_reply
        services = load_services(_v2_root())
        prof = propose_profile(intent, services, cluster=args.cluster)
        prof.secret_seed = []
        prof.tls = args.tls
        prof.platform = args.platform
        client = None
        try:
            from skwire import resolve_client
            client = resolve_client()
        except Exception:
            client = None
        print("🌱 " + concierge_reply(intent, prof, services, client=client) + "\n")
        if not prof.services:
            return 0
    elif args.services:
        prof.services = [s.strip() for s in args.services.split(",") if s.strip()]
    for kv in args.seed:
        k, _, v = kv.partition("=")
        prof.secret_seed.append({"key": k, "value": v})
    if args.cmd == "serve":
        from .web.server import serve
        serve()
        return 0

    if args.cmd == "rotation":
        from .rotation import rotation_plan
        plan = rotation_plan(_v2_root(), prof.services, tls=prof.tls)
        if not plan:
            print("Nothing to rotate yet — add services (--services / an intent), --tls for certs.")
            return 0
        print(f"🔁 rotation plan for '{prof.cluster}' ({len(plan)} item(s)):")
        for r in plan:
            tag = "🔐 secret" if r.kind == "secret" else "📜 cert"
            print(f"  {tag}  {r.service}/{r.target:30} every {r.every_days}d  "
                  f"[{'automatic' if r.automatic else 'manual'}]")
            print(f"            ↳ {r.how}")
        return 0

    store = StateStore(_state_path())
    steps = install_flow(prof, run=system_runner, v2_root=_v2_root())

    if args.cmd == "plan":
        print(f"skbloom plan — {len(steps)} steps for cluster '{prof.cluster}':")
        for s in steps:
            mark = "✓" if store.is_done(s.name) else " "
            print(f"  [{mark}] {s.name:22} {s.description}")
        return 0
    if args.cmd == "status":
        done = store.completed()
        print(f"completed {len(done)}/{len(steps)}: {', '.join(done) or '(none)'}")
        pending = [s.name for s in steps if not store.is_done(s.name)]
        print(f"pending: {', '.join(pending) or '(none — all done)'}")
        return 0
    if args.cmd == "reset":
        store.reset()
        print("progress cleared (cluster left intact).")
        return 0

    # up / resume
    print(f"🌱 skbloom up — sovereign stack on '{prof.cluster}'")
    from .flow import service_urls
    store.set_meta({"urls": service_urls(prof, _v2_root()), "domain": prof.domain, "tls": prof.tls})
    results = run_steps(steps, store, on_event=_narrate)
    failed = [r for r in results if r.status == "failed"]
    if failed:
        print(f"\n✗ stopped at '{failed[0].name}': {failed[0].error}")
        print("  fix the cause, then `skbloom resume` to continue from here.")
        return 1
    print(f"\n✅ done — {len(store.completed())} steps complete. `skbloom status` for details.")
    if prof.tls and prof.services and prof.platform == "k8s":
        print("\n🔗 your services (HTTPS, auto-rotating certs):")
        for svc in prof.services:
            nm = svc.split("=", 1)[1] if "=" in svc else svc.rstrip("/").split("/")[-1]
            print(f"   https://{nm}.{prof.domain}")
        print("   trust the CA in your browser:")
        print("     kubectl get secret skbloom-ca-tls -n cert-manager "
              "-o jsonpath='{.data.tls\\.crt}' | base64 -d > skbloom-ca.crt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
