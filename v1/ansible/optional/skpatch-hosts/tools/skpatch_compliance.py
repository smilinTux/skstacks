#!/usr/bin/env python3
"""skpatch-hosts fleet compliance: is every host provisioned exactly as the
template says, and does the dashboard agree?

One read-only pass, nothing is changed anywhere:

  1. `ansible-inventory --list` with the same -i/-l you provision with. Hosts
     with host var skpatch_hosts_paused: true are reported as PAUSED and
     never contacted (they are added to the limit as `!host`).
  2. `ansible-playbook provision_skpatch_hosts_nodes.yml --check` against the
     rest, with skpatch_hosts_compliance_report set, so the SAME template and
     tasks that provision a host decide whether it drifted, and the
     registration play reads the dashboard (systems, detected managers,
     latest sudo failures) through its read-only report step.
  3. Issues, keyed host|issue:
       unreachable           the host did not answer ssh
       paused-marker-file    paused by /etc/skpatch-hosts.paused on the host
       not-provisioned       the skpatch account would be created
       drift                 any provisioning task would change something
                             (sudoers, wrappers, key, conffile policy)
       uncovered-manager     the host (or its dashboard system) has a package
                             manager sudo-coverage.yml does not cover
       check-failed          another provisioning task failed
       dashboard-only        a dashboard system no inventory host matches
       not-in-dashboard      a provisioned host the dashboard does not know
       sudo-denied:<action>  the dashboard's latest <action> on that system
                             failed on sudo asking for a password
  4. De-duplicated alerts: a state file remembers open issues; a hook is
     called once when an issue opens (`raise`) and once when it clears
     (`resolve`), never on every run.

Usage (the provisioning command's own -i/-l/-e/vault args after `--`):

    skpatch_compliance.py --state /var/lib/skpatch-compliance/state.json \\
        --alert-hook /usr/local/bin/skpatch-compliance-alert \\
        -- -i shared/hosts -i shared/skpatch_targets.ini -l skpatch_hosts \\
           -e skpatch_hosts_register_manager_group=<managers> \\
           -e skstacks_vault_dir=$PWD --vault-password-file <file>

The hook is run as `<hook> raise|resolve <host|issue> <warn|info> <message>`.
Exit status: 0 no open issues, 1 open issues, 2 the check itself failed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROLE = Path(__file__).resolve().parents[1]
PLAYBOOK = ROLE / "provision_skpatch_hosts_nodes.yml"
ACCOUNT_TASK = "Create the skpatch-hosts account (present)"
REFUSE_TASK = "Refuse a host with an uncovered package manager"
PAUSE_TASK = "Report the pause"
PROVISION_PLAY_SKIP = {"Report the posture for this host"}


def load_supported() -> list[str]:
    import yaml  # ansible's own dependency, always present where this runs

    return yaml.safe_load((ROLE / "src" / "skpatch-hosts" / "sudo-coverage.yml").read_text())["supported"]


def split_args(args: list[str]) -> tuple[list[str], str | None, list[str]]:
    """Split the passthrough args into (inventory args, limit, everything else)."""
    inv, rest, limit = [], [], None
    it = iter(args)
    for a in it:
        if a in ("-i", "--inventory", "--inventory-file"):
            inv += [a, next(it)]
        elif a.startswith("--inventory="):
            inv.append(a)
        elif a in ("-l", "--limit"):
            limit = next(it)
        elif a.startswith("--limit="):
            limit = a.split("=", 1)[1]
        else:
            if a in ("--vault-password-file", "--vault-id"):
                v = next(it)
                inv += [a, v]
                rest += [a, v]
                continue
            rest.append(a)
    return inv, limit, rest


def inventory(inv_args: list[str], limit: str | None) -> dict[str, dict]:
    """hostvars of every host in the limit, without contacting any host."""
    cmd = ["ansible-inventory", *inv_args, "--list"] + (["--limit", limit] if limit else [])
    data = json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)
    hostvars = data.get("_meta", {}).get("hostvars", {})
    hosts: set[str] = set()

    def walk(group: str, seen: set[str]) -> None:
        if group in seen:
            return
        seen.add(group)
        g = data.get(group, {})
        hosts.update(g.get("hosts", []))
        for child in g.get("children", []):
            walk(child, seen)

    walk("all", set())
    return {h: hostvars.get(h, {}) for h in sorted(hosts)}


def with_exclusions(limit: str | None, exclude: set[str]) -> str:
    """`limit` plus `!host` for each excluded host, as one Ansible pattern.

    Ansible splits a pattern on commas when it has any, else on colons, so a
    "group:!host" limit cannot just get ",!other" appended: it would become one
    unmatched pattern. Normalise to commas first."""
    base = limit or "all"
    parts = base.split(",") if "," in base else base.split(":")
    return ",".join([p for p in parts if p] + [f"!{h}" for h in sorted(exclude)])


def truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "yes", "1", "on")


def run_check(playbook: Path, inv_args, limit, rest, report_path: Path) -> dict:
    env = dict(os.environ, ANSIBLE_STDOUT_CALLBACK="ansible.posix.json", ANSIBLE_NOCOLOR="1",
               ANSIBLE_DEPRECATION_WARNINGS="False", ANSIBLE_RETRY_FILES_ENABLED="False")
    cmd = ["ansible-playbook", str(playbook), "--check", *inv_args, *rest,
           "-e", f"skpatch_hosts_compliance_report={report_path}"] + (["--limit", limit] if limit else [])
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    start = proc.stdout.find("{")
    if start < 0:
        raise RuntimeError(f"ansible-playbook printed no JSON (rc {proc.returncode}):\n{proc.stderr[-3000:]}")
    return json.loads(proc.stdout[start:])


def analyse(result: dict, hosts: dict[str, dict], paused: set[str], report: dict | None,
            supported: list[str], outside: dict[str, dict] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """Return (issues {host|issue: message}, status {host: summary}).

    `outside` holds inventory hosts the --limit left out: a dashboard system
    that matches one is reported as "not checked", never as dashboard-only."""
    issues: dict[str, str] = {}
    status: dict[str, str] = {h: "paused (host var)" for h in paused}
    plays = result.get("plays", [])
    stats = result.get("stats", {})
    provision = plays[0] if plays else {"tasks": []}
    changed: dict[str, list[str]] = {}
    marker_paused: set[str] = set()
    for task in provision.get("tasks", []):
        name = task.get("task", {}).get("name", "")
        for host, r in task.get("hosts", {}).items():
            if name == PAUSE_TASK and not r.get("skipped"):
                marker_paused.add(host)
                status[host] = "paused (/etc/skpatch-hosts.paused)"
                issues[f"{host}|paused-marker-file"] = "paused by /etc/skpatch-hosts.paused on the host (remove it to resume)"
            if r.get("unreachable"):
                issues[f"{host}|unreachable"] = f"ssh failed: {str(r.get('msg', ''))[:200]}"
            elif r.get("failed"):
                if name == REFUSE_TASK:
                    issues[f"{host}|uncovered-manager"] = str(r.get("msg", ""))[:400]
                else:
                    issues[f"{host}|check-failed"] = f"{name}: {str(r.get('msg', ''))[:300]}"
            elif r.get("changed") and name not in PROVISION_PLAY_SKIP:
                changed.setdefault(host, []).append(name)
    for host, names in changed.items():
        if ACCOUNT_TASK in names:
            issues[f"{host}|not-provisioned"] = "the skpatch account does not exist yet: run the provisioning playbook"
        else:
            issues[f"{host}|drift"] = "provisioning would change: " + "; ".join(names)
    for host, s in stats.items():
        if s.get("unreachable") and f"{host}|unreachable" not in issues:
            issues[f"{host}|unreachable"] = "ssh failed"
        status.setdefault(host, "issues" if any(k.startswith(host + "|") for k in issues) else "compliant")
    if report is None:
        issues["dashboard|no-report"] = ("the dashboard was not read (registration not configured, "
                                         "or the registration play failed): see the ansible output")
        return issues, status

    outside = outside or {}
    by_name = {h: h for h in list(hosts) + list(outside)}
    by_addr = {}
    for h, hv in list(outside.items()) + list(hosts.items()):
        for addr in (hv.get("skpatch_hosts_dashboard_address"), hv.get("ansible_host"), h):
            if addr:
                by_addr[str(addr)] = h
    matched: set[str] = set()
    for s in report.get("systems", []):
        host = by_name.get(s["name"]) or by_addr.get(s.get("hostname", ""))
        label = host or s["name"]
        if host is None:
            issues[f"{label}|dashboard-only"] = (f"dashboard system {s['name']} ({s.get('hostname')}) matches no host "
                                                 "in this inventory and limit: provision it, or remove it from the dashboard")
            continue
        if host not in hosts:
            status[host] = "not checked (outside --limit)"
            continue
        matched.add(host)
        if host in paused or host in marker_paused:
            continue
        active = [m for m in s.get("detected", []) if m not in s.get("disabled", [])]
        uncovered = sorted(set(active) - set(supported))
        if uncovered:
            issues[f"{host}|uncovered-manager"] = (f"dashboard detected {', '.join(uncovered)} (not covered by the sudoers "
                                                   "template): disable it for this system or add coverage")
        for d in s.get("sudo_denied", []):
            issues[f"{host}|sudo-denied:{d['action']}"] = (f"{d['action']} ({d.get('pkgManager') or '-'}) at {d['at']}: "
                                                           f"{d['error']}")
    for host in hosts:
        if host in paused or host in marker_paused or host in matched:
            continue
        if any(f"{host}|{k}" in issues for k in ("unreachable", "not-provisioned", "check-failed", "uncovered-manager")):
            continue
        issues[f"{host}|not-in-dashboard"] = "provisioned, but no dashboard system has this name or address"
    for host in hosts:
        if host not in paused and host not in marker_paused:
            status[host] = "issues" if any(k.startswith(host + "|") for k in issues) else "compliant"
    return issues, status


def alert(hook: str | None, action: str, key: str, level: str, message: str) -> None:
    if not hook:
        return
    try:
        subprocess.run([*shlex.split(hook), action, key, level, message], timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as err:
        print(f"alert hook failed for {key}: {err}", file=sys.stderr)


def reconcile(state_path: Path | None, issues: dict[str, str], hook: str | None) -> dict:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    state = {}
    if state_path and state_path.exists():
        state = json.loads(state_path.read_text() or "{}")
    opened = [k for k in issues if k not in state]
    closed = [k for k in state if k not in issues]
    for k in sorted(opened):
        alert(hook, "raise", k, "warn", f"skpatch compliance: {k.split('|')[0]}: {issues[k]}")
        state[k] = {"since": now, "message": issues[k]}
    for k in sorted(closed):
        alert(hook, "resolve", k, "info", f"skpatch compliance: {k} is resolved")
        del state[k]
    for k in issues:
        state[k]["message"] = issues[k]
    if state_path:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        tmp.replace(state_path)
    return {"opened": sorted(opened), "closed": sorted(closed)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Arguments after -- go to ansible-inventory/ansible-playbook.")
    ap.add_argument("--state", type=Path, help="de-duplication state file (open issues)")
    ap.add_argument("--alert-hook", help="command run as: <hook> raise|resolve <key> <level> <message>")
    ap.add_argument("--json-out", type=Path, help="write the full result here")
    ap.add_argument("--playbook", type=Path, default=PLAYBOOK)
    ap.add_argument("passthrough", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    passthrough = args.passthrough[1:] if args.passthrough[:1] == ["--"] else args.passthrough

    inv_args, limit, rest = split_args(passthrough)
    try:
        hosts = inventory(inv_args, limit)
        outside = {h: hv for h, hv in inventory(inv_args, None).items() if h not in hosts} if limit else {}
        paused = {h for h, hv in hosts.items() if truthy(hv.get("skpatch_hosts_paused", False))}
        active_limit = with_exclusions(limit, paused)
        with tempfile.TemporaryDirectory(prefix="skpatch-compliance-") as tmp:
            report_path = Path(tmp) / "dashboard.json"
            result = run_check(args.playbook, inv_args, active_limit, rest, report_path)
            report = json.loads(report_path.read_text()) if report_path.exists() else None
    except (subprocess.CalledProcessError, RuntimeError, json.JSONDecodeError, OSError) as err:
        detail = getattr(err, "stderr", "") or ""
        print(f"compliance check failed: {err}\n{detail[-2000:]}", file=sys.stderr)
        return 2

    issues, status = analyse(result, hosts, paused, report, load_supported(), outside)
    changes = reconcile(args.state, issues, args.alert_hook)

    width = max([len(h) for h in status] + [4])
    print(f"{'host':<{width}}  status")
    for host in sorted(status):
        print(f"{host:<{width}}  {status[host]}")
        for k in sorted(k for k in issues if k.startswith(host + "|")):
            print(f"{'':<{width}}    {k.split('|', 1)[1]}: {issues[k]}")
    for k in sorted(k for k in issues if k.split("|")[0] not in status):
        print(f"{k}: {issues[k]}")
    print(f"\n{len(issues)} open issue(s); opened {len(changes['opened'])}, resolved {len(changes['closed'])}; "
          f"{len(paused)} paused")
    if args.json_out:
        args.json_out.write_text(json.dumps({"status": status, "issues": issues, **changes,
                                             "paused": sorted(paused)}, indent=2, sort_keys=True) + "\n")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
