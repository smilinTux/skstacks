# skred — the self-hardening security loop

> *Try to break our own stuff, on a schedule, and fix what we find — without ever
> touching anything that isn't ours.*

skred runs best-of-breed open-source security scanners through a **scope-locked**
orchestrator, normalizes every tool's output into one comparable shape, **gates CI**
by severity, and closes the loop with **LLM-suggested fixes**.

## The safety rail (non-negotiable)
Offensive tooling only ever touches **our own estate**. Every target — path, host, IP,
or URL — is checked against a `ScopeGuard` (own domains / CIDRs / repo roots) **before**
any scanner runs. Unknown target → refused. Empty allowlist → refuses everything
(**fail closed**). Defaults live in `config.py` and are loopback-only; declare your estate with `SKRED_SCOPE_DOMAINS` / `SKRED_SCOPE_CIDRS`.

## What it wraps (and what's easy to add)
| Scanner | Catches |
|---------|---------|
| **gitleaks** | committed secrets (we've been burned — this is the watchdog) |
| **trivy** | dependency CVEs, IaC misconfig, container + fs secrets |
| **semgrep** | SAST / dangerous code patterns |

Adding nuclei / checkov / kube-bench is one `Scanner` adapter (build cmd → parse JSON
→ `Finding`). Tools are optional — `available()` skips any that aren't installed.

## Use it
```bash
skred scan .                  # scope-guarded scan of the repo (human output)
skred report . --json         # machine-readable findings
skred gate . --fail-on high   # exit non-zero if any finding >= HIGH (the CI gate)
skred scan . --fix            # enrich each fix via the local LLM (skwire's model ladder)
```

## The loop in CI
`.github/workflows/skred-scan.yml` installs the scanners and runs `skred gate` on every
PR/push + nightly. HIGH+ findings fail the build; the normalized report is uploaded as
an artifact. With a local model wired, `--fix` turns findings into concrete remediations
("pin go.mod to X and run `go mod tidy`").

## Architecture (mirrors skwire)
Pure-stdlib core, dependency-injected runners, everything unit-tested with fixtures (no
tool install needed to test the parsers). `scope` → `scanners` → `orchestrator` →
`findings`/`gate` → `remediate`.

**Status:** scope guard, gitleaks/trivy/semgrep adapters, orchestrator, gate, LLM
remediation, CLI, and the CI workflow — all shipped + tested. Next: auto-PR remediation,
nuclei/kube-bench adapters, and active (scoped) exploit verification.
