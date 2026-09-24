"""
skred — the self-hardening security loop for SKStacks.

Scope-locked red-team/scanning: best-of-breed OSS scanners (gitleaks/trivy/semgrep...)
run ONLY against our own estate (ScopeGuard, fail-closed), normalize into one Finding
shape, gate CI by severity, and close the loop with LLM-suggested fixes.

    from skred import default_guard, run_scans, ALL_SCANNERS, gate, Severity, remediate
"""
from __future__ import annotations

from .scope import ScopeGuard, OutOfScopeError
from .findings import Finding, Severity, dedupe, rank, gate, GateResult
from .scanners import (
    Scanner, GitleaksScanner, TrivyScanner, SemgrepScanner,
    ALL_SCANNERS, FakeRunner, system_runner, RunResult,
)
from .orchestrator import run_scans, ScanReport
from .config import default_guard, OWN_DOMAINS, OWN_CIDRS
from .remediate import remediate

__version__ = "0.1.0"

__all__ = [
    "ScopeGuard", "OutOfScopeError",
    "Finding", "Severity", "dedupe", "rank", "gate", "GateResult",
    "Scanner", "GitleaksScanner", "TrivyScanner", "SemgrepScanner",
    "ALL_SCANNERS", "FakeRunner", "system_runner", "RunResult",
    "run_scans", "ScanReport",
    "default_guard", "OWN_DOMAINS", "OWN_CIDRS",
    "remediate", "__version__",
]
