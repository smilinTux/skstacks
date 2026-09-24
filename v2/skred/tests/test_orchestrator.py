"""The orchestrator must enforce scope on EVERY target before any scanner runs,
then collect/dedupe/rank findings across all scanners."""
from __future__ import annotations

import pytest

from skred.scope import ScopeGuard, OutOfScopeError
from skred.findings import Severity
from skred.orchestrator import run_scans, ScanReport


class StubScanner:
    def __init__(self, name, findings):
        self.name = name; self.binary = name; self._f = findings; self.scanned = []
    def available(self, which=None):
        return True
    def scan(self, target, *, run=None):
        self.scanned.append(target)
        return list(self._f)


def _finding(sev, fp=None):
    from skred.findings import Finding
    return Finding(scanner="stub", severity=sev, title="t", target="a", fingerprint=fp)


@pytest.fixture
def guard():
    return ScopeGuard(domains=["skworld.io"], cidrs=["192.168.100.0/24"], roots=["/tmp/skstacks-build"])


def test_only_in_scope_targets_are_scanned(guard):
    sc = StubScanner("s", [_finding(Severity.LOW)])
    report = run_scans(["192.168.100.5", "8.8.8.8", "skwire.skworld.io"], [sc], guard)
    assert "192.168.100.5" in sc.scanned and "skwire.skworld.io" in sc.scanned
    assert "8.8.8.8" not in sc.scanned                 # external target NEVER scanned
    assert "8.8.8.8" in report.skipped_out_of_scope


def test_strict_mode_raises_on_out_of_scope(guard):
    with pytest.raises(OutOfScopeError):
        run_scans(["8.8.8.8"], [StubScanner("s", [])], guard, strict=True)


def test_findings_are_merged_deduped_and_ranked(guard):
    s1 = StubScanner("a", [_finding(Severity.LOW, fp="dup"), _finding(Severity.CRITICAL)])
    s2 = StubScanner("b", [_finding(Severity.LOW, fp="dup")])     # same fp → deduped
    report = run_scans(["192.168.100.5"], [s1, s2], guard)
    assert isinstance(report, ScanReport)
    assert report.findings[0].severity is Severity.CRITICAL       # ranked worst-first
    assert len(report.findings) == 2                              # dup collapsed


def test_unavailable_scanners_are_skipped(guard):
    class Missing(StubScanner):
        def available(self, which=None): return False
    m = Missing("missing", [_finding(Severity.HIGH)])
    report = run_scans(["192.168.100.5"], [m], guard)
    assert report.findings == [] and "missing" in report.skipped_scanners


# ── baseline: known debt must be suppressible, new leaks must NOT be ──────────
from skred.orchestrator import load_baseline, suppress_known
from skred.findings import Finding, Severity


def _f(fp):
    return Finding(scanner="gitleaks", severity=Severity.HIGH, title="secret",
                   target="a.yml", line=1, description="", remediation="",
                   fingerprint=fp)


def test_baseline_suppresses_known_but_keeps_new():
    """The whole point of the gate: old debt is quiet, a NEW secret still fails."""
    kept = suppress_known([_f("old:a.yml:rule:1"), _f("new:b.yml:rule:2")],
                          {"old:a.yml:rule:1"})
    assert [f.fingerprint for f in kept] == ["new:b.yml:rule:2"]


def test_empty_baseline_suppresses_nothing():
    """A missing baseline must fail OPEN into full reporting, never silence."""
    findings = [_f("x"), _f("y")]
    assert suppress_known(findings, set()) == findings


def test_load_baseline_missing_file_is_empty(tmp_path):
    assert load_baseline(str(tmp_path / "nope.txt")) == set()


def test_load_baseline_ignores_comments_and_blanks(tmp_path):
    f = tmp_path / "b.txt"
    f.write_text("# a comment\n\nabc:1\n  def:2  \n", encoding="utf-8")
    assert load_baseline(str(f)) == {"abc:1", "def:2"}
