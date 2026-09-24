"""Guard tests for the security scan pipeline + gates + closed-loop + red-team."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_SEC = Path(__file__).resolve().parents[1]                      # cicd/security
_SKSEC = _SEC.parents[1] / "core" / "sksec"


def test_scan_pipeline_covers_all_stages():
    doc = yaml.safe_load((_SEC / "scan.yml").read_text())
    jobs = set(doc["jobs"])
    assert {"secrets", "sast", "iac", "sca-image", "k8s-posture"} <= jobs
    blob = (_SEC / "scan.yml").read_text()
    for tool in ("gitleaks", "semgrep", "trivy", "grype", "syft", "checkov", "kubescape"):
        assert tool in blob.lower(), f"scanner {tool} missing"


def test_scan_runs_nightly_not_just_on_push():
    doc = yaml.safe_load((_SEC / "scan.yml").read_text())
    # NB: YAML parses `on:` as the boolean True key (GH Actions quirk).
    triggers = doc.get(True) or doc.get("on")
    assert "schedule" in triggers and "cron" in str(triggers)


def test_gates_block_critical_and_secrets_never_auto_merge():
    g = yaml.safe_load((_SEC / "security-gates.yaml").read_text())["spec"]
    assert "CRITICAL" in g["block_on"]["cve_severity"]
    assert g["block_on"]["secret_findings"] == "any"
    # the cardinal rule: closed loop may open PRs but NEVER auto-merge
    assert g["closed_loop"]["auto_merge"] == []
    assert g["closed_loop"]["require_passing_rescan"] is True


def test_closed_loop_documents_human_gate_and_rollback():
    r = (_SKSEC / "closed-loop" / "README.md").read_text().lower()
    assert "never" in r and "auto-merge" in r
    assert "rollback" in r and "argocd" in r


def test_redteam_is_scope_locked_and_authorized():
    r = (_SKSEC / "redteam" / "README.md").read_text().lower()
    assert "scope" in r and "own" in r                 # owned targets only
    assert "non-destructive" in r and "kill-switch" in r
    assert "no merge" in r or "no merge/deploy rights" in r
