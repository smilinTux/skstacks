#!/usr/bin/env python3
"""skpatch-hosts dashboard watch: the immediate alert classes the dashboard
cannot send itself, and the daily summary. Read-only, dashboard only.

The dashboard's own notification channels cover "host unreachable" (immediate)
and "updates available" (07:00 digest). This tool covers the rest by reading
the dashboard through the same GET-only report the compliance check uses
(src/skpatch-hosts/dashboard_report.js, run inside the dashboard container
over ssh). It never contacts a managed host.

Watch mode (every ~15 min), issues keyed host|issue, raised once and resolved
once through the alert hook (the same de-duplication as skpatch_compliance.py,
its own state file):

  upgrade-failed:<action>  the latest <action> (upgrade_all, upgrade_package,
                           full_upgrade, reboot, ...) failed, not on sudo
  sudo-denied:<action>     the latest <action> failed on sudo asking for a
                           password (a provisioning gap, its own class)
  package-manager          the dashboard reports package manager issues
                           (APT needs repair, a held dpkg lock, ...)
  reboot-pending           a reboot has been required for more than
                           --reboot-days (default 8) on the same boot; the
                           normal "reboot required" is never an alert

Digest mode (--digest, once a day): one message with OS end-of-life and
support warnings, the last 24 hours of scheduled runs, pending update totals
and the issues still open.

Usage:
    skpatch_watch.py --ssh <dashboard node> --register-as <admin> \\
        --state ~/.local/state/skpatch-watch/<estate>.json \\
        --alert-hook tools/skpatch-talk-notify.sh [--digest]
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT_JS = HERE.parent / "src" / "skpatch-hosts" / "dashboard_report.js"
MARK = "@@SKP-REPORT@@"
QUIET_LIFECYCLE = {None, "", "supported", "unknown"}

_spec = importlib.util.spec_from_file_location("skpatch_compliance", HERE / "skpatch_compliance.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


def remote_command(container: str) -> str:
    js = base64.b64encode(REPORT_JS.read_bytes()).decode()
    return ("set -eu; D=docker; $D info >/dev/null 2>&1 || D='sudo -n docker'; "
            f"C=$($D ps -q --filter name={shlex.quote(container)} | head -n1); "
            "[ -n \"$C\" ] || { echo 'no dashboard container on this node' >&2; exit 3; }; "
            f"$D exec -i \"$C\" node --no-warnings -e \"$(echo {js} | base64 -d)\"")


def fetch_report(ssh: list[str], container: str, register_as: str, history_days: int) -> dict:
    proc = subprocess.run([*ssh, remote_command(container)], input=json.dumps(
        {"register_as": register_as, "history_days": history_days}), capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"report failed (exit {proc.returncode}): {proc.stderr.strip()[-800:]}")
    line = next((l for l in proc.stdout.splitlines() if l.startswith(MARK)), None)
    if line is None:
        raise RuntimeError("report printed no result line")
    return json.loads(line[len(MARK):])


def watch_issues(report: dict, ages: dict, now: dt.datetime, reboot_days: float) -> tuple[dict, dict, dict]:
    """Return (issues, levels, new_ages). ages maps host -> {boot_id, since}:
    when a reboot was first seen required on that boot."""
    issues: dict[str, str] = {}
    levels: dict[str, str] = {}
    new_ages: dict[str, dict] = {}
    for s in report.get("systems", []):
        host = s["name"]
        for f in s.get("failed", []):
            k = f"{host}|upgrade-failed:{f['action']}"
            issues[k] = f"{f['action']} failed at {f['at']}: {f['error']}"
            levels[k] = "error"
        for d in s.get("sudo_denied", []):
            k = f"{host}|sudo-denied:{d['action']}"
            issues[k] = (f"{d['action']} at {d['at']}: {d['error']} (the sudoers rule does not cover what the "
                         "dashboard ran: re-run the provisioning playbook for this host)")
            levels[k] = "error"
        if s.get("package_issues", 0) > 0:
            k = f"{host}|package-manager"
            issues[k] = f"{s['package_issues']} package manager issue(s) (open the system in the dashboard: Repair)"
        if s.get("needs_reboot"):
            prev = ages.get(host)
            boot = s.get("boot_id")
            since = prev["since"] if prev and prev.get("boot_id") == boot else now.isoformat(timespec="seconds")
            new_ages[host] = {"boot_id": boot, "since": since}
            days = (now - dt.datetime.fromisoformat(since)).total_seconds() / 86400
            if days > reboot_days:
                issues[f"{host}|reboot-pending"] = f"reboot required for {days:.0f} days (since {since[:10]})"
    return issues, levels, new_ages


def lifecycle_lines(report: dict, warn_days: int) -> list[str]:
    out = []
    for s in report.get("systems", []):
        lc = s.get("lifecycle") or {}
        status = lc.get("status")
        notes = []
        for label, days_key, date_key in (("end of life", "days_to_eol", "eol"),
                                          ("end of standard support", "days_to_support_end", "support_end")):
            days = lc.get(days_key)
            if isinstance(days, (int, float)) and days <= warn_days:
                when = f"{-days} days ago" if days < 0 else f"in {days} days"
                notes.append(f"{label} {when} ({lc.get(date_key)})")
        if status not in QUIET_LIFECYCLE or notes:
            name = lc.get("label") or "OS"
            out.append(f"- {s['name']}: {name}, {status or 'unknown'}" + (f", {'; '.join(notes)}" if notes else ""))
    return out


def digest_text(report: dict, open_issues: dict, now: dt.datetime, warn_days: int, title: str) -> str:
    lines = [f"📋 {title}, {now.astimezone().strftime('%a %Y-%m-%d')}"]
    systems = report.get("systems", [])
    pending = [s for s in systems if s.get("updates", 0) > 0]
    total = sum(s["updates"] for s in pending)
    sec = sum(s.get("security", 0) for s in pending)
    lines.append(f"\nUpdates: {total} on {len(pending)} of {len(systems)} systems" + (f", {sec} security" if sec else ""))
    lines += [f"- {s['name']}: {s['updates']}" + (f" ({s['security']} security)" if s.get("security") else "")
              for s in sorted(pending, key=lambda s: -s["updates"])]
    since = now - dt.timedelta(hours=24)
    runs = [d for d in report.get("schedules", []) if d.get("enabled") and d.get("type") in ("update", "reboot")
            and d.get("last_run_at") and dt.datetime.fromisoformat(d["last_run_at"].replace("Z", "+00:00")) >= since]
    lines.append(f"\nLast 24 hours of scheduled runs: {len(runs)}")
    lines += [f"- {'✅' if d['status'] == 'success' else '⚠️'} {d['name']}: {d['message'] or d['status']}"
              for d in sorted(runs, key=lambda d: d["last_run_at"])]
    lc = lifecycle_lines(report, warn_days)
    lines.append("\nOS lifecycle: " + ("no warnings" if not lc else f"{len(lc)} warning(s)"))
    lines += lc
    lines.append(f"\nOpen issues: {len(open_issues)}")
    lines += [f"- {k}" for k in sorted(open_issues)]
    unreachable = [s["name"] for s in systems if s.get("reachable") is False]
    if unreachable:
        lines.append(f"\nUnreachable now: {', '.join(sorted(unreachable))}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ssh", required=True, help="ssh destination of a node running the dashboard container")
    ap.add_argument("--ssh-option", action="append", default=[], help="extra ssh argument (repeatable)")
    ap.add_argument("--container", default="skpatch-hosts-prod_dashboard.", help="docker ps name filter")
    ap.add_argument("--register-as", required=True, help="existing dashboard admin (REGISTER_AS)")
    ap.add_argument("--state", type=Path, required=True, help="state file (open issues; reboot ages next to it)")
    ap.add_argument("--alert-hook", help="command run as: <hook> raise|resolve|post <key> <level> <message>")
    ap.add_argument("--reboot-days", type=float, default=8)
    ap.add_argument("--history-days", type=int, default=3)
    ap.add_argument("--lifecycle-warn-days", type=int, default=90)
    ap.add_argument("--digest", action="store_true", help="post the daily summary instead of watching")
    ap.add_argument("--title", default="skpatch daily summary")
    args = ap.parse_args()

    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", *args.ssh_option, args.ssh]
    try:
        report = fetch_report(ssh, args.container, args.register_as, args.history_days)
    except (RuntimeError, subprocess.SubprocessError, OSError, json.JSONDecodeError) as err:
        print(f"watch failed: {err}", file=sys.stderr)
        return 2
    now = dt.datetime.now(dt.timezone.utc)
    ages_path = args.state.with_name(args.state.stem + ".reboot.json")
    ages = json.loads(ages_path.read_text() or "{}") if ages_path.exists() else {}
    issues, levels, new_ages = watch_issues(report, ages, now, args.reboot_days)

    if args.digest:
        open_issues = json.loads(args.state.read_text() or "{}") if args.state.exists() else {}
        text = digest_text(report, open_issues, now, args.lifecycle_warn_days, args.title)
        print(text)
        if args.alert_hook and not sc.alert(args.alert_hook, "post", "digest", "info", text):
            return 2
        return 0

    changes = sc.reconcile(args.state, issues, args.alert_hook, label="skpatch", levels=levels)
    ages_path.parent.mkdir(parents=True, exist_ok=True)
    ages_path.write_text(json.dumps(new_ages, indent=2, sort_keys=True) + "\n")
    for k in sorted(issues):
        print(f"{k}: {issues[k]}")
    print(f"{len(issues)} open issue(s); opened {len(changes['opened'])}, resolved {len(changes['closed'])}")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
