"""Normalized findings: every scanner's output funnels into one comparable shape,
deduped and ranked by severity so the gate and the report speak one language."""
from __future__ import annotations

import pytest

from skred.findings import Finding, Severity, dedupe, rank, gate, GateResult


def _f(sev, scanner="x", title="t", target="a.py", line=1, fp=None):
    return Finding(scanner=scanner, severity=sev, title=title, target=target, line=line,
                   fingerprint=fp)


def test_severity_orders_critical_highest():
    assert Severity.CRITICAL > Severity.HIGH > Severity.MEDIUM > Severity.LOW > Severity.INFO


def test_from_str_is_tolerant_of_scanner_vocab():
    assert Severity.from_str("CRITICAL") is Severity.CRITICAL
    assert Severity.from_str("error") is Severity.HIGH         # semgrep "ERROR"
    assert Severity.from_str("warning") is Severity.MEDIUM
    assert Severity.from_str("moderate") is Severity.MEDIUM
    assert Severity.from_str("unknown-word") is Severity.INFO  # fail safe, never crash


def test_rank_sorts_worst_first():
    out = rank([_f(Severity.LOW), _f(Severity.CRITICAL), _f(Severity.MEDIUM)])
    assert [f.severity for f in out] == [Severity.CRITICAL, Severity.MEDIUM, Severity.LOW]


def test_dedupe_collapses_same_fingerprint():
    a = _f(Severity.HIGH, fp="dup"); b = _f(Severity.HIGH, fp="dup"); c = _f(Severity.LOW, fp="other")
    assert len(dedupe([a, b, c])) == 2


def test_dedupe_falls_back_to_natural_key_when_no_fingerprint():
    # same scanner+target+line+title => same finding even without an explicit fingerprint
    a = _f(Severity.HIGH, scanner="trivy", title="CVE-1", target="img", line=0)
    b = _f(Severity.HIGH, scanner="trivy", title="CVE-1", target="img", line=0)
    assert len(dedupe([a, b])) == 1


def test_gate_fails_when_a_finding_meets_threshold():
    res = gate([_f(Severity.HIGH), _f(Severity.LOW)], threshold=Severity.HIGH)
    assert isinstance(res, GateResult)
    assert res.passed is False and res.blocking[0].severity is Severity.HIGH


def test_gate_passes_when_all_below_threshold():
    res = gate([_f(Severity.MEDIUM), _f(Severity.LOW)], threshold=Severity.HIGH)
    assert res.passed is True and res.blocking == []


def test_gate_on_empty_findings_passes():
    assert gate([], threshold=Severity.LOW).passed is True
