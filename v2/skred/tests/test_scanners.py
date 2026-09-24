"""Scanner adapters wrap best-of-breed OSS tools and normalize their output. We test
the PARSERS against real tool JSON shapes (no tool install needed) + the runner wiring."""
from __future__ import annotations

import json

from skred.findings import Severity
from skred.scanners import GitleaksScanner, TrivyScanner, SemgrepScanner, FakeRunner


# ── gitleaks: secret scanning (we leaked creds before — this is the watchdog) ──
GITLEAKS_JSON = json.dumps([
    {"Description": "GitHub PAT", "File": "deploy.sh", "StartLine": 12,
     "RuleID": "github-pat", "Secret": "ghp_xxx", "Fingerprint": "deploy.sh:github-pat:12"},
])


def test_gitleaks_parses_secrets_as_high_severity():
    s = GitleaksScanner()
    runner = FakeRunner(stdout=GITLEAKS_JSON, returncode=1)   # gitleaks exits 1 when it finds leaks
    found = s.scan("/tmp/skstacks-build", run=runner)
    assert len(found) == 1
    f = found[0]
    assert f.scanner == "gitleaks" and f.severity >= Severity.HIGH
    assert f.target == "deploy.sh" and f.line == 12
    assert f.fingerprint == "deploy.sh:github-pat:12"


def test_gitleaks_clean_run_yields_nothing():
    assert GitleaksScanner().scan("/x", run=FakeRunner(stdout="[]", returncode=0)) == []


# ── trivy: vuln + misconfig + secret scanning over fs/images/IaC ──
TRIVY_JSON = json.dumps({"Results": [
    {"Target": "Dockerfile", "Misconfigurations": [
        {"ID": "DS002", "Title": "root user", "Severity": "HIGH", "Resolution": "USER nonroot"}]},
    {"Target": "go.mod", "Vulnerabilities": [
        {"VulnerabilityID": "CVE-2026-1", "Title": "RCE", "Severity": "CRITICAL", "PkgName": "x"}]},
]})


def test_trivy_parses_vulns_and_misconfigs():
    found = TrivyScanner().scan("/repo", run=FakeRunner(stdout=TRIVY_JSON, returncode=0))
    sevs = sorted(f.severity for f in found)
    assert Severity.CRITICAL in sevs and Severity.HIGH in sevs
    assert any("CVE-2026-1" in f.title for f in found)
    assert any(f.remediation for f in found)          # carries the resolution hint


# ── semgrep: SAST ──
SEMGREP_JSON = json.dumps({"results": [
    {"check_id": "py.subprocess-shell-true", "path": "app.py",
     "start": {"line": 42}, "extra": {"severity": "ERROR", "message": "shell=True is risky"}},
]})


def test_semgrep_maps_error_to_high():
    found = SemgrepScanner().scan("/repo", run=FakeRunner(stdout=SEMGREP_JSON, returncode=1))
    assert found and found[0].severity is Severity.HIGH
    assert found[0].target == "app.py" and found[0].line == 42


def test_scanner_handles_garbage_output_without_crashing():
    assert GitleaksScanner().scan("/x", run=FakeRunner(stdout="not json", returncode=2)) == []


def test_available_reflects_whether_the_tool_is_installed():
    assert TrivyScanner().available(which=lambda b: "/usr/bin/trivy") is True
    assert TrivyScanner().available(which=lambda b: None) is False
