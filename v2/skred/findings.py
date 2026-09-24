"""
skred findings — one normalized shape for every scanner's output.

Each tool (gitleaks, trivy, semgrep, checkov, …) speaks its own dialect of severity
and JSON. We funnel them all into `Finding`, rank by `Severity`, dedupe, and gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


class Severity(IntEnum):
    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def from_str(cls, s: str) -> "Severity":
        """Map any scanner's severity word to our scale. Fail safe → INFO."""
        t = (s or "").strip().lower()
        return {
            "critical": cls.CRITICAL, "crit": cls.CRITICAL,
            "high": cls.HIGH, "error": cls.HIGH, "severe": cls.HIGH,
            "medium": cls.MEDIUM, "moderate": cls.MEDIUM, "warning": cls.MEDIUM, "warn": cls.MEDIUM,
            "low": cls.LOW, "minor": cls.LOW, "note": cls.LOW,
            "info": cls.INFO, "informational": cls.INFO, "unknown": cls.INFO,
        }.get(t, cls.INFO)


@dataclass
class Finding:
    scanner: str
    severity: Severity
    title: str
    target: str                         # file path, image, host, or URL it was found on
    line: int = 0
    description: str = ""
    remediation: str = ""               # filled by the closed-loop remediator
    fingerprint: Optional[str] = None   # scanner-provided stable id, if any
    raw: dict = field(default_factory=dict)

    def key(self) -> str:
        """Dedupe key: the scanner fingerprint if present, else a natural composite."""
        if self.fingerprint:
            return f"fp:{self.fingerprint}"
        return f"{self.scanner}|{self.target}|{self.line}|{self.title}"


def dedupe(findings) -> list:
    seen: dict = {}
    for f in findings:
        seen.setdefault(f.key(), f)     # first one wins
    return list(seen.values())


def rank(findings) -> list:
    """Worst first, stable within a severity."""
    return sorted(findings, key=lambda f: f.severity, reverse=True)


@dataclass
class GateResult:
    passed: bool
    blocking: list = field(default_factory=list)   # findings at/above the threshold
    threshold: Severity = Severity.HIGH

    def summary(self) -> str:
        if self.passed:
            return "✓ skred gate passed — no findings at or above the threshold."
        return (f"✗ skred gate FAILED — {len(self.blocking)} finding(s) at or above "
                f"{self.threshold.name}.")


def gate(findings, threshold: Severity = Severity.HIGH) -> GateResult:
    """Fail the build if any finding meets/exceeds the severity threshold."""
    blocking = rank([f for f in findings if f.severity >= threshold])
    return GateResult(passed=not blocking, blocking=blocking, threshold=threshold)
