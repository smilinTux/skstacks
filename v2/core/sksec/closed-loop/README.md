# sksec / closed-loop — continuous scan → patch → deploy → verify

A one-time CI gate isn't enough: new CVEs land on already-deployed artifacts daily.
The closed loop keeps the **whole stack** continuously patched — fast, with a human
(or CAB) only in the approve seat, never in the toil.

```
   ┌────────────────────────────────────────────────────────────────────────┐
   │  DETECT        nightly scan.yml + SBOM-vs-new-CVE match (skcicd/security)│
   │     │          + skmon/Falco runtime + sksec NIDS + the red-team (below) │
   │     ▼                                                                    │
   │  TRIAGE        local LLM (qwen/Ollama) ranks by exploitability + reach,  │
   │     │          dedups, checks security-gates.yaml (block vs warn vs SLA) │
   │     ▼                                                                    │
   │  REMEDIATE     agent drafts a fix: dependency/base-image bump, IaC patch,│
   │     │          config hardening → opens a PR (NEVER auto-merge)          │
   │     ▼                                                                    │
   │  VERIFY        CI re-scan must come back clean + tests green (gate)      │
   │     │                                                                    │
   │  APPROVE       human / CAB merges (itil_cab_vote) — the only manual step │
   │     ▼                                                                    │
   │  DEPLOY        ArgoCD reconciles (K8s) / Ansible (Swarm) from the repo   │
   │     │                                                                    │
   │  CONFIRM       post-deploy re-scan + skpulse/skmon healthy → close ITIL  │
   └───────────────────────────────┘  loop  └───────────────────────────────┘
```

## How it maps to what we already have
- **DETECT** — `skcicd/security/scan.yml` + retained SBOMs + Falco/Wazuh/CrowdSec
  (sksec) + skmon alerts. New-CVE-vs-SBOM matching means zero-day-to-PR in minutes.
- **TRIAGE/REMEDIATE** — the **skbloom agent-ops** brain (Phase-5 upgrade/CVE agent),
  driven by a *local* model (sovereign, offline-capable). It only ever produces a
  **PR + a descriptor/manifest change** — deterministic skos/ArgoCD does the apply.
- **APPROVE** — `security-gates.yaml.closed_loop.auto_merge: []` → **never auto-merge**;
  CRITICAL = same-day SLA via `itil_change_propose` + `itil_cab_vote`.
- **DEPLOY/CONFIRM** — ArgoCD app-of-apps + skops ITIL incident/change tracking;
  Gatus/skpulse confirms health, closes the loop.

## Guardrails (non-negotiable)
- The agent **drafts, never merges**. Every change is a reviewed PR with a clean
  re-scan + passing tests required.
- Auto-actions are limited to `auto_pr` classes in the gate policy (dep/image bumps,
  IaC fixes) — no schema/secret/identity changes unattended.
- Full audit trail (PR + ITIL change record + SARIF history); reversible via Git.
- Rollback is a `git revert` → ArgoCD reconcile.
