#!/usr/bin/env python3
"""skpatch-hosts dashboard watch: the immediate alert classes the dashboard
cannot send itself, and the daily summary. Read-only, dashboard only.

The dashboard's own notification channels cover "host unreachable" (immediate)
and "updates available" (07:00 digest). This tool covers the rest by reading
the dashboard through the same GET-only report the compliance check uses
(src/skpatch-hosts/dashboard_report.js, run inside the dashboard container
over ssh). It never contacts a managed host.

Watch mode (every ~15 min), issues keyed host|issue, raised once and resolved
once through the alert hook (the same deduplication as skpatch_compliance.py,
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
support warnings, the last 24 hours of scheduled runs, pending update totals,
the vulnerabilities (pending security updates per host and, with
--image-vulns, the HIGH/CRITICAL fixable findings of the swarm's images from
skpatch_image_vulns.py) and the issues still open. The main message keeps
counts and the top items; the full vulnerability list is posted as a reply
in the same thread (the hook prints the message id), cut to
--max-message-chars with an "and N more" line.

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
import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT_JS = HERE.parent / "src" / "skpatch-hosts" / "dashboard_report.js"
MARK = "@@SKP-REPORT@@"
COVER = "@@SKP-COVERAGE@@"
KNOWN_GAPS = HERE.parent / "src" / "skpatch-hosts" / "known-gaps.json"
QUIET_LIFECYCLE = {None, "", "supported", "unknown"}
# Nextcloud Talk refuses a chat message over 32000 characters; leave room
# for the hook's own prefix.
MAX_MESSAGE_CHARS = 30000
TOP_ITEMS = 5
IMAGE_SCAN_STALE_HOURS = 36

_spec = importlib.util.spec_from_file_location("skpatch_compliance", HERE / "skpatch_compliance.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


def remote_command(container: str, coverage: str) -> str:
    js = base64.b64encode(REPORT_JS.read_bytes()).decode()
    return ("set -eu; D=docker; $D info >/dev/null 2>&1 || D='sudo -n docker'; "
            f"C=$($D ps -q --filter name={shlex.quote(container)} | head -n1); "
            "[ -n \"$C\" ] || { echo 'no dashboard container on this node' >&2; exit 3; }; "
            f"printf '{COVER}'; cat {shlex.quote(coverage)} 2>/dev/null | tr -d '\\n' || true; echo; "
            f"$D exec -i \"$C\" node --no-warnings -e \"$(echo {js} | base64 -d)\"")


def fetch_report(ssh: list[str], container: str, register_as: str, history_days: int,
                 coverage: str = "/var/data/config/skpatch-hosts-prod/skpatch-coverage.json") -> dict:
    proc = subprocess.run([*ssh, remote_command(container, coverage)], input=json.dumps(
        {"register_as": register_as, "history_days": history_days}), capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"report failed (exit {proc.returncode}): {proc.stderr.strip()[-800:]}")
    line = next((l for l in proc.stdout.splitlines() if l.startswith(MARK)), None)
    if line is None:
        raise RuntimeError("report printed no result line")
    report = json.loads(line[len(MARK):])
    cov = next((l[len(COVER):] for l in proc.stdout.splitlines() if l.startswith(COVER)), "")
    try:
        report["coverage"] = json.loads(cov) if cov.strip() else {}
    except ValueError:
        report["coverage"] = {}
    return report


def _when(iso: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


def _short(iso: str | None) -> str:
    t = _when(iso)
    return t.strftime("%Y-%m-%d %H:%MZ") if t else "?"


def load_known_gaps() -> dict:
    try:
        return json.loads(KNOWN_GAPS.read_text()).get("sudo-denied", {})
    except (OSError, ValueError):
        return {}


def _block(head: str, s: dict, lines: list[str], next_action: str, url: str | None) -> str:
    """One alert body: what happened, the evidence, where, what to do."""
    out = [head]
    if lines:
        out += ["```"] + lines + ["```"]
    if url:
        out.append(f"Dashboard: {url.rstrip('/')}/systems/{s['id']}")
    out.append(f"Next: {next_action}")
    return "\n".join(out)


def watch_issues(report: dict, ages: dict, now: dt.datetime, reboot_days: float,
                 url: str | None = None, gaps: dict | None = None) -> tuple[dict, dict, dict]:
    """Return (issues, levels, new_ages). ages maps host -> {boot_id, since}:
    when a reboot was first seen required on that boot. The report already
    drops history older than the last good run of the same operation."""
    gaps = load_known_gaps() if gaps is None else gaps
    coverage = report.get("coverage") or {}
    issues: dict[str, str] = {}
    levels: dict[str, str] = {}
    new_ages: dict[str, dict] = {}
    for s in report.get("systems", []):
        host = s["name"]
        s.setdefault("id", 0)
        for f in s.get("failed", []):
            k = f"{host}|upgrade-failed:{f['action']}"
            label = "last lines of the run (it was cut off, no error line in what the app kept)" \
                if f.get("tail_is_raw") else "last lines of the run"
            issues[k] = _block(f"{f['action']} failed at {_short(f['at'])}; {label}:", s, f.get("tail") or [f.get("error", "")],
                               f"open the run in the system's history, fix the cause, then run {f['action']} again. "
                               "This resolves by itself on the next successful upgrade or clean check.", url)
            levels[k] = "error"
        stamp = coverage.get(host) or {}
        for d in s.get("sudo_denied", []):
            covered_at = _when(stamp.get("provisioned_at"))
            if covered_at and covered_at > _when(d["at"]) and (stamp.get("actions") or d["action"] == "check"):
                continue  # sudoers was rewritten since: resolved
            k = f"{host}|sudo-denied:{d['action']}"
            cmd = [f"$ sudo {d['command'][:200]}" + (" ..." if len(d["command"]) > 200 else "")] if d.get("command") else []
            gap = gaps.get(d["action"])
            if gap:
                head = f"{d['action']} at {_short(d['at'])} was refused by sudo: {d['error']}. Known gap: {gap['gap']}."
                nxt = f"known gap, fix in {gap['fix']}; until then {gap['until']}."
                levels[k] = "warn"
            else:
                head = f"{d['action']} at {_short(d['at'])} was refused by sudo: {d['error']}"
                nxt = (f"provision this host again (provision_skpatch_hosts_nodes.yml -l {host}, --check --diff first); "
                       "if it is already current, the sudoers template is missing this command: report it.")
                levels[k] = "error"
            issues[k] = _block(head, s, cmd, nxt, url)
        for i in s.get("package_issue_list") or ([{}] if s.get("package_issues", 0) > 0 else []):
            k = f"{host}|package-manager"
            title = i.get("title") or f"{s['package_issues']} package manager issue(s)"
            msg = [i["message"]] if i.get("message") else []
            since = f" since {_short(i['detected_at'])}" if i.get("detected_at") else ""
            issues[k] = _block(f"{title}{since}:", s, msg,
                               "press Repair on the system page (it runs dpkg --configure -a). If Repair succeeds and the "
                               "banner comes back, read `dpkg --audit` on the host: the app treats any audit output as "
                               "\"interrupted\". This resolves by itself when the banner is gone.", url)
            break
        if s.get("needs_reboot"):
            prev = ages.get(host)
            boot = s.get("boot_id")
            since = prev["since"] if prev and prev.get("boot_id") == boot else now.isoformat(timespec="seconds")
            new_ages[host] = {"boot_id": boot, "since": since}
            days = (now - dt.datetime.fromisoformat(since)).total_seconds() / 86400
            if days > reboot_days:
                issues[f"{host}|reboot-pending"] = _block(
                    f"reboot required for {days:.0f} days (since {since[:10]}):", s, [],
                    "reboot it in its maintenance window (or from the system page); this resolves after the reboot.", url)
    return issues, levels, new_ages


def resolutions(report: dict, keys: list[str]) -> dict[str, str]:
    """A short reason for each issue that just cleared."""
    by = {s["name"]: s for s in report.get("systems", [])}
    cov = report.get("coverage") or {}
    out = {}
    for k in keys:
        host, _, issue = k.partition("|")
        s = by.get(host)
        if s is None:
            out[k] = "the system is no longer in the dashboard"
            continue
        kind, _, action = issue.partition(":")
        ok = s.get("last_ok") or {}
        if kind == "upgrade-failed":
            if ok.get(action):
                out[k] = f"{action} succeeded at {_short(ok[action])}"
            else:
                later = max([v for a, v in ok.items() if a != "check" and v] or [""])
                out[k] = (f"an upgrade succeeded at {_short(later)}" if later else
                          f"a clean check at {_short(s.get('last_clean_check'))}")
        elif kind == "sudo-denied":
            stamp = (cov.get(host) or {}).get("provisioned_at")
            out[k] = (f"{action} succeeded at {_short(ok[action])}" if ok.get(action) else
                      f"sudoers now covers it (provisioned {_short(stamp)})" if stamp else "the denial is gone")
        elif kind == "package-manager":
            out[k] = "the dashboard no longer reports a package manager issue"
        elif kind == "reboot-pending":
            out[k] = "rebooted (or no longer required)"
        else:
            out[k] = "cleared"
    return out


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


def fit(lines: list[str], limit: int) -> str:
    """Join lines, keeping whole lines under limit; what does not fit becomes
    one "... and N more lines" line (room for it is always kept)."""
    out: list[str] = []
    size = 0
    for i, line in enumerate(lines):
        rest = len(lines) - i
        tail = f"... and {rest} more line{'s' if rest != 1 else ''} (cut to fit one Talk message)"
        need = len(line) + 1 + (0 if rest == 1 else len(tail) + 1)
        if size + need > limit:
            out.append(tail)
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def security_hosts(report: dict) -> list[dict]:
    """Hosts with pending security updates, by name."""
    return sorted((s for s in report.get("systems", []) if s.get("security", 0) > 0 or s.get("security_updates")),
                  key=lambda s: s["name"])


def _pkgs(s: dict, n: int | None = None) -> str:
    names = [u["package"] for u in s.get("security_updates") or []]
    if not names:
        return f"{s.get('security', 0)} security update(s), names not in this report"
    if n is None or len(names) <= n:
        return ", ".join(names)
    return ", ".join(names[:n]) + f" (+{len(names) - n} more)"


def load_image_vulns(path: Path | None) -> dict | None:
    if not path:
        return None
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {"error": f"no scan result at {path} (has skpatch-image-vulns run?)"}
    except (OSError, ValueError) as err:
        return {"error": f"unreadable scan result {path}: {err}"}


def _img(i: dict) -> str:
    return i["image"].partition("@")[0]


def _ranked(images: list[dict]) -> list[dict]:
    return sorted((i for i in images if i.get("status") == "ok" and (i.get("critical") or i.get("high"))),
                  key=lambda i: (-i.get("critical", 0), -i.get("high", 0), _img(i)))


def vuln_summary_lines(report: dict, images: dict | None, now: dt.datetime, top: int = TOP_ITEMS) -> list[str]:
    """The compact part for the main message: counts and the top items."""
    hosts = security_hosts(report)
    n = sum(len(s.get("security_updates") or []) or s.get("security", 0) for s in hosts)
    lines = [f"\nVulnerabilities (pending security updates): " + (f"{n} on {len(hosts)} host(s)" if hosts else "none")]
    lines += [f"- {s['name']}: {_pkgs(s, 3)}" for s in hosts[:top]]
    if len(hosts) > top:
        lines.append(f"- and {len(hosts) - top} more host(s)")
    if images is None:
        return lines
    if images.get("error"):
        return lines + [f"\nImage vulnerabilities: scan failed: {images['error']}"]
    t = images.get("totals") or {}
    head = (f"\nImage vulnerabilities: {t.get('critical', 0)} critical, {t.get('high', 0)} high (fix available)"
            f" in {len(_ranked(images.get('images', [])))} of {t.get('images', 0)} images")
    if t.get("ignored"):
        head += f", {t['ignored']} ignored"
    at = images.get("generated_at")
    if at and now - dt.datetime.fromisoformat(at) > dt.timedelta(hours=IMAGE_SCAN_STALE_HOURS):
        head += f" (stale: last scan {_short(at)})"
    lines.append(head)
    ranked = _ranked(images.get("images", []))
    lines += [f"- {_img(i)} ({', '.join(i.get('services') or []) or 'no service'}): "
              f"{i.get('critical', 0)} critical, {i.get('high', 0)} high" for i in ranked[:top]]
    if len(ranked) > top:
        lines.append(f"- and {len(ranked) - top} more image(s)")
    failed = [i for i in images.get("images", []) if i.get("status") == "failed"]
    if failed:
        names = ", ".join(_img(i) for i in failed[:top]) + (f" and {len(failed) - top} more" if len(failed) > top else "")
        lines.append(f"- scan failed for {len(failed)} image(s): {names}")
    return lines


def vuln_detail_lines(report: dict, images: dict | None) -> list[str]:
    """The full list, for the reply. Empty when there is nothing to list."""
    lines: list[str] = []
    hosts = security_hosts(report)
    if hosts:
        lines.append("Security updates by host:")
        for s in hosts:
            ups = s.get("security_updates") or []
            lines.append(f"{s['name']}: {len(ups) or s.get('security', 0)}")
            lines += [f"- {u['package']} {u.get('current') or '?'} -> {u.get('new') or '?'}"
                      + (f" ({u['pkgManager']})" if u.get("pkgManager") else "") for u in ups]
            if not ups:
                lines.append("- (package names not in this report)")
    if images and not images.get("error"):
        ranked = _ranked(images.get("images", []))
        failed = [i for i in images.get("images", []) if i.get("status") == "failed"]
        if ranked or failed:
            lines.append(("\n" if lines else "") + "Image vulnerabilities (fix available):")
        for i in ranked:
            lines.append(f"{_img(i)} ({', '.join(i.get('services') or [])}): "
                         f"{i.get('critical', 0)} critical, {i.get('high', 0)} high")
            lines += [f"- {v['severity']} {v['id']} {v['package']} {v.get('installed') or '?'} -> {v.get('fixed') or '?'}"
                      for v in i.get("vulns", [])]
        for i in failed:
            lines.append(f"{_img(i)}: scan failed: {i.get('error') or 'unknown error'}")
    return lines


def digest_detail(report: dict, images: dict | None, title: str, limit: int = MAX_MESSAGE_CHARS) -> str | None:
    lines = vuln_detail_lines(report, images)
    if not lines:
        return None
    return fit([f"🔎 {title}: vulnerabilities, full list"] + lines, limit)


def digest_text(report: dict, open_issues: dict, now: dt.datetime, warn_days: int, title: str,
                images: dict | None = None, limit: int = MAX_MESSAGE_CHARS) -> str:
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
    lines += vuln_summary_lines(report, images, now)
    lc = lifecycle_lines(report, warn_days)
    lines.append("\nOS lifecycle: " + ("no warnings" if not lc else f"{len(lc)} warning(s)"))
    lines += lc
    lines.append(f"\nOpen issues: {len(open_issues)}")
    lines += [f"- {k}" for k in sorted(open_issues)]
    unreachable = [s["name"] for s in systems if s.get("reachable") is False]
    if unreachable:
        lines.append(f"\nUnreachable now: {', '.join(sorted(unreachable))}")
    return fit("\n".join(lines).split("\n"), limit)


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
    ap.add_argument("--dashboard-url", help="dashboard base URL, for /systems/<id> links in alerts")
    ap.add_argument("--image-vulns", type=Path, default=os.environ.get("SKPW_IMAGE_VULNS") or None,
                    help="summary from skpatch_image_vulns.py, for the digest (default $SKPW_IMAGE_VULNS)")
    ap.add_argument("--max-message-chars", type=int, default=MAX_MESSAGE_CHARS)
    ap.add_argument("--coverage-path", default="/var/data/config/skpatch-hosts-prod/skpatch-coverage.json",
                    help="sudo coverage stamp on the dashboard's node (written by provisioning)")
    args = ap.parse_args()

    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", *args.ssh_option, args.ssh]
    try:
        report = fetch_report(ssh, args.container, args.register_as, args.history_days, args.coverage_path)
    except (RuntimeError, subprocess.SubprocessError, OSError, json.JSONDecodeError) as err:
        print(f"watch failed: {err}", file=sys.stderr)
        return 2
    now = dt.datetime.now(dt.timezone.utc)
    ages_path = args.state.with_name(args.state.stem + ".reboot.json")
    ages = json.loads(ages_path.read_text() or "{}") if ages_path.exists() else {}
    issues, levels, new_ages = watch_issues(report, ages, now, args.reboot_days, args.dashboard_url)

    if args.digest:
        open_issues = json.loads(args.state.read_text() or "{}") if args.state.exists() else {}
        images = load_image_vulns(args.image_vulns.expanduser() if args.image_vulns else None)
        text = digest_text(report, open_issues, now, args.lifecycle_warn_days, args.title, images,
                           args.max_message_chars)
        detail = digest_detail(report, images, args.title, args.max_message_chars)
        print(text)
        if detail:
            print("\n" + detail)
        if not args.alert_hook:
            return 0
        out = sc.alert(args.alert_hook, "post", "digest", "info", text)
        if out is None:
            return 2
        if detail:
            ref = out.splitlines()[-1].strip() if out else ""
            if sc.alert(args.alert_hook, "post", "digest-detail", "info", detail,
                        ref if ref.isdigit() else None) is None:
                return 2
        return 0

    prev = json.loads(args.state.read_text() or "{}") if args.state.exists() else {}
    reasons = resolutions(report, [k for k in prev if k not in issues])
    changes = sc.reconcile(args.state, issues, args.alert_hook, label="", levels=levels, reasons=reasons)
    ages_path.parent.mkdir(parents=True, exist_ok=True)
    ages_path.write_text(json.dumps(new_ages, indent=2, sort_keys=True) + "\n")
    for k in sorted(issues):
        print(f"{k}:\n{issues[k]}\n")
    print(f"{len(issues)} open issue(s); opened {len(changes['opened'])}, resolved {len(changes['closed'])}")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
