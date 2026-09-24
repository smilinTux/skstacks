"""The closed loop: turn findings into concrete fix suggestions. Uses an injected
LLM client when present (reusing skwire's model ladder), else the built-in hints."""
from __future__ import annotations

from skred.findings import Finding, Severity
from skred.remediate import remediate


def _f(rem=""):
    return Finding(scanner="trivy", severity=Severity.HIGH, title="CVE-1", target="go.mod",
                   remediation=rem)


def test_without_a_model_keeps_builtin_hints(monkeypatch):
    monkeypatch.setenv("SKWIRE_LLM_DISABLE", "1")        # force the no-model path
    out = remediate([_f(rem="bump to v1.2.3")], client=None)
    assert out[0].remediation == "bump to v1.2.3"


def test_with_a_model_enriches_the_remediation():
    class FakeLLM:
        name = "fake"
        def chat(self, messages):
            return "Pin go.mod to the patched release and run `go mod tidy`."
    out = remediate([_f()], client=FakeLLM())
    assert "go mod tidy" in out[0].remediation


def test_model_failure_falls_back_to_existing_hint():
    class BrokenLLM:
        name = "broken"
        def chat(self, messages):
            raise RuntimeError("model down")
    out = remediate([_f(rem="existing hint")], client=BrokenLLM())
    assert out[0].remediation == "existing hint"        # never crashes the scan
