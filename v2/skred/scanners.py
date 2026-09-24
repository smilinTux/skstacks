"""
skred scanners — adapters over best-of-breed OSS security tools.

Each adapter builds the tool's command, runs it (via an injected runner so it's
testable + so the orchestrator can enforce scope), and parses the tool's native JSON
into normalized `Finding`s. Tools are optional: `available()` reports whether the
binary is installed, so a CI image with only some tools still works.

Wrapped today: gitleaks (secrets), trivy (vuln/misconfig/secret over fs/IaC/images),
semgrep (SAST). The Protocol makes adding nuclei/checkov/kube-bench trivial.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .findings import Finding, Severity


@dataclass
class RunResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


class FakeRunner:
    """Test double: returns canned output and records the command it was given."""
    def __init__(self, stdout="", stderr="", returncode=0):
        self._r = RunResult(stdout, stderr, returncode)
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return self._r


def system_runner(cmd, timeout=900, **kw) -> RunResult:
    """Real subprocess runner (no shell — argv list only)."""
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)  # nosec - argv, no shell
    return RunResult(p.stdout, p.stderr, p.returncode)


@runtime_checkable
class Scanner(Protocol):
    name: str
    binary: str
    def available(self, which=...) -> bool: ...
    def scan(self, target: str, *, run=...) -> list: ...


class _Base:
    name = "base"
    binary = "true"

    def available(self, which=shutil.which) -> bool:
        return which(self.binary) is not None

    def _run(self, cmd, run):
        run = run or system_runner
        try:
            return run(cmd)
        except Exception as e:                       # tool crash/timeout → no findings
            return RunResult(stderr=str(e), returncode=-1)

    @staticmethod
    def _json(text):
        try:
            return json.loads(text or "")
        except (ValueError, TypeError):
            return None


class GitleaksScanner(_Base):
    name = "gitleaks"
    binary = "gitleaks"

    def scan(self, target: str, *, run=None) -> list:
        cmd = ["gitleaks", "detect", "--source", target, "--no-banner",
               "--report-format", "json", "--report-path", "/dev/stdout"]
        res = self._run(cmd, run)
        data = self._json(res.stdout)
        if not isinstance(data, list):
            return []
        out = []
        for d in data:
            out.append(Finding(
                scanner=self.name, severity=Severity.HIGH,        # any real secret = HIGH+
                title=d.get("Description") or d.get("RuleID") or "secret",
                target=d.get("File", target), line=int(d.get("StartLine", 0) or 0),
                description=f"rule={d.get('RuleID','?')}",
                remediation="rotate the secret + purge from history; move it to the secret store",
                fingerprint=d.get("Fingerprint"), raw=d))
        return out


class TrivyScanner(_Base):
    name = "trivy"
    binary = "trivy"

    def scan(self, target: str, *, run=None) -> list:
        cmd = ["trivy", "fs", "--quiet", "--format", "json",
               "--scanners", "vuln,misconfig,secret", target]
        res = self._run(cmd, run)
        data = self._json(res.stdout)
        if not isinstance(data, dict):
            return []
        out = []
        for r in data.get("Results", []) or []:
            tgt = r.get("Target", target)
            for v in r.get("Vulnerabilities", []) or []:
                out.append(Finding(
                    scanner=self.name, severity=Severity.from_str(v.get("Severity", "")),
                    title=f"{v.get('VulnerabilityID','CVE')}: {v.get('Title','')}".strip(": "),
                    target=tgt, description=f"pkg={v.get('PkgName','?')}",
                    remediation=v.get("FixedVersion", ""), fingerprint=v.get("VulnerabilityID"), raw=v))
            for m in r.get("Misconfigurations", []) or []:
                out.append(Finding(
                    scanner=self.name, severity=Severity.from_str(m.get("Severity", "")),
                    title=f"{m.get('ID','')}: {m.get('Title','')}".strip(": "),
                    target=tgt, remediation=m.get("Resolution", ""),
                    fingerprint=m.get("ID"), raw=m))
            for s in r.get("Secrets", []) or []:
                out.append(Finding(
                    scanner=self.name, severity=Severity.from_str(s.get("Severity", "high")),
                    title=s.get("Title", "secret"), target=tgt, line=int(s.get("StartLine", 0) or 0),
                    remediation="rotate + move to the secret store", raw=s))
        return out


class SemgrepScanner(_Base):
    name = "semgrep"
    binary = "semgrep"

    def scan(self, target: str, *, run=None) -> list:
        cmd = ["semgrep", "--json", "--quiet", "--config", "auto", target]
        res = self._run(cmd, run)
        data = self._json(res.stdout)
        if not isinstance(data, dict):
            return []
        out = []
        for r in data.get("results", []) or []:
            extra = r.get("extra", {}) or {}
            out.append(Finding(
                scanner=self.name, severity=Severity.from_str(extra.get("severity", "")),
                title=r.get("check_id", "rule"), target=r.get("path", target),
                line=int((r.get("start", {}) or {}).get("line", 0) or 0),
                description=extra.get("message", ""), fingerprint=r.get("check_id"), raw=r))
        return out


ALL_SCANNERS = [GitleaksScanner, TrivyScanner, SemgrepScanner]
