"""
skred orchestrator — run the scanners safely and collate the results.

The one inviolable rule: every target is checked against the ScopeGuard BEFORE any
scanner touches it. Out-of-scope targets are skipped (or, in strict mode, raise).
Then findings from all scanners are merged, deduped, and ranked worst-first.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from .findings import dedupe, rank
from .scope import ScopeGuard, OutOfScopeError



# ── baseline ──────────────────────────────────────────────────────────────────
# A repo with existing leaks fails the gate on day one and keeps failing. A gate
# that is always red stops being read, which is worse than no gate because it
# still looks like coverage. So: suppress the KNOWN debt by fingerprint and let
# the gate fail only on NEW findings. Shrink the file as history gets cleaned.
#
# Fingerprints only (commit:file:rule:line). Never the secret itself -- gitleaks'
# own --baseline-path needs the full record including the cleartext Secret and
# Match fields, so committing one of those would put every leaked credential
# back into the repo in the clear.
BASELINE_ENV = "SKRED_BASELINE"
BASELINE_DEFAULT = ".skred/baseline-fingerprints.txt"


def load_baseline(path: str | None = None) -> set:
    """Fingerprints to treat as known debt. Missing file = empty set = suppress nothing."""
    path = path or os.environ.get(BASELINE_ENV) or BASELINE_DEFAULT
    try:
        with open(path, encoding="utf-8") as fh:
            return {ln.strip() for ln in fh
                    if ln.strip() and not ln.lstrip().startswith("#")}
    except OSError:
        return set()


def suppress_known(findings, baseline: set) -> list:
    """Drop findings whose fingerprint is already accepted debt."""
    if not baseline:
        return list(findings)
    return [f for f in findings if getattr(f, "fingerprint", None) not in baseline]

@dataclass
class ScanReport:
    findings: list = field(default_factory=list)
    scanned_targets: list = field(default_factory=list)
    skipped_out_of_scope: list = field(default_factory=list)
    skipped_scanners: list = field(default_factory=list)

    def counts(self) -> dict:
        c: dict = {}
        for f in self.findings:
            c[f.severity.name] = c.get(f.severity.name, 0) + 1
        return c


def run_scans(targets, scanners, guard: ScopeGuard, *, strict: bool = False,
              run=None, which=shutil.which) -> ScanReport:
    """Scan every IN-SCOPE target with every AVAILABLE scanner."""
    report = ScanReport()

    # 1) scope gate — fail closed. Nothing offensive runs against a non-allowed target.
    in_scope = []
    for t in targets:
        if guard.is_allowed(t):
            in_scope.append(t)
        else:
            report.skipped_out_of_scope.append(t)
            if strict:
                raise OutOfScopeError(f"target {t!r} is out of scope")

    # 2) only run installed scanners
    live = []
    for s in scanners:
        if s.available(which=which):
            live.append(s)
        else:
            report.skipped_scanners.append(s.name)

    # 3) scan
    collected = []
    for t in in_scope:
        report.scanned_targets.append(t)
        for s in live:
            collected.extend(s.scan(t, run=run))

    report.findings = rank(dedupe(suppress_known(collected, load_baseline())))
    return report
