"""
skred CLI — the self-hardening loop you can run locally or in CI.

    skred scan [PATH ...]            # scope-guarded scan of our own code/infra
    skred gate [PATH ...] --fail-on high   # exit non-zero if findings >= severity (CI gate)
    skred report [PATH ...] --json  # machine-readable findings

Targets default to the current repo. Out-of-scope targets are refused (fail-closed).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .config import default_guard
from .findings import Severity, gate as gate_fn
from .orchestrator import run_scans
from .scanners import ALL_SCANNERS
from .remediate import remediate


def _force_utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _run(targets, fix=False):
    guard = default_guard()
    scanners = [c() for c in ALL_SCANNERS]
    report = run_scans(targets, scanners, guard)
    if fix:
        report.findings = remediate(report.findings)
    return report


def _print_human(report):
    if report.skipped_scanners:
        print(f"· scanners not installed (skipped): {', '.join(report.skipped_scanners)}")
    if report.skipped_out_of_scope:
        print(f"· refused {len(report.skipped_out_of_scope)} out-of-scope target(s) 🛡️")
    if not report.findings:
        print("✓ no findings.")
        return
    print(f"\n{len(report.findings)} finding(s): {report.counts()}\n")
    for f in report.findings:
        loc = f"{f.target}:{f.line}" if f.line else f.target
        print(f"  [{f.severity.name:8}] {f.scanner:9} {f.title}")
        print(f"             ↳ {loc}")
        if f.remediation:
            print(f"             ↳ fix: {f.remediation}")


def main(argv=None) -> int:
    _force_utf8()
    p = argparse.ArgumentParser(prog="skred", description="Self-hardening security loop (scope-locked).")
    sub = p.add_subparsers(dest="cmd")
    for name in ("scan", "gate", "report"):
        sp = sub.add_parser(name)
        sp.add_argument("targets", nargs="*", default=[], help="paths/hosts (default: this repo)")
        sp.add_argument("--fail-on", default="high", choices=[s.name.lower() for s in Severity])
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        sp.add_argument("--fix", action="store_true", help="enrich remediation via the local LLM")
    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 0

    # normalise filesystem targets to absolute paths so "." / "src" resolve correctly
    raw = args.targets or [os.getcwd()]
    targets = [os.path.realpath(t) if os.path.exists(t) else t for t in raw]
    report = _run(targets, fix=getattr(args, "fix", False))

    if getattr(args, "json", False) or args.cmd == "report":
        print(json.dumps({
            "counts": report.counts(),
            "scanned": report.scanned_targets,
            "refused_out_of_scope": report.skipped_out_of_scope,
            "findings": [{"scanner": f.scanner, "severity": f.severity.name, "title": f.title,
                          "target": f.target, "line": f.line, "remediation": f.remediation}
                         for f in report.findings],
        }, indent=2))
    else:
        _print_human(report)

    if args.cmd == "gate":
        res = gate_fn(report.findings, threshold=Severity.from_str(args.fail_on))
        print("\n" + res.summary())
        return 0 if res.passed else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
