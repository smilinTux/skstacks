# skcicd / security — scanners in the pipeline

Every push/PR + nightly runs `scan.yml`. Findings gate the deploy per
`security-gates.yaml` and feed the **closed loop** (`core/sksec/closed-loop/`).
Designed for fast iteration: scan early, fail only on what matters, auto-remediate
the rest.

| Stage | Tools | Catches |
|---|---|---|
| **secrets** | gitleaks + trufflehog (verified) | committed creds/keys |
| **SAST** | Semgrep (ci/security-audit/secrets) | code vulns/anti-patterns |
| **IaC** | Trivy config + Checkov | k8s/compose/tofu/dockerfile misconfig |
| **SBOM + SCA** | Syft (SBOM) → Grype + Trivy fs | dependency + image CVEs |
| **K8s posture** | Kubescape (NSA/CIS) | cluster-hardening gaps |

Nightly re-scan matters: **new CVEs land on already-shipped artifacts**, so a clean
build today can be vulnerable tomorrow — that's what makes the *closed loop* (not
just a one-time gate) necessary. SARIF is uploaded for trend tracking; SBOMs are
retained so a new CVE can be matched against every deployed artifact instantly.

All tools are OSS and self-hostable (none phone home); the same stages render to
Forgejo Actions / GitLab CI / GitHub Actions via the skcicd SCM adapter.
