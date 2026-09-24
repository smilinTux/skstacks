# sksec / redteam (skred) — authorized autonomous self-red-team

Continuous, AI-driven offensive testing of **our own** deployed environments, as an
ongoing hardening effort. The defenders (scanners, Falco, CrowdSec) tell you what's
*theoretically* wrong; the red-team tells you what's *actually exploitable* — and
feeds confirmed findings straight into the closed loop.

> **Authorization + scope (hard guardrails).** skred runs **only** against
> SKStacks-owned targets explicitly listed in its scope file — never third-party
> systems, never the public internet. It is an authorized security exercise on
> infrastructure the operator owns. Out-of-scope targets are refused by construction.

## Architecture
```
  scope.yaml (OWN targets only) ──► skred orchestrator ──► local offensive model
        │                                  │                 (qwen/abliterated, Ollama;
        │                                  │                  sovereign, offline)
        ▼                                  ▼
  isolated replica env  ◄── attack ── tool-calling: recon → enumerate → exploit-attempt
  (a throwaway clone of the stack,        │   (nmap, nuclei, ZAP, sqlmap, custom MCP tools)
   NOT live prod where possible)          ▼
                              confirmed finding (with PoC + repro)
                                           │
                                           ▼
                              → sksec/closed-loop (TRIAGE) → remediation PR → re-test
```

## Why a local / abliterated model
- **Sovereign + offline** — offensive prompts and our infra details never leave the
  box (a hosted model would be a data-exfil + refusal problem).
- An **abliterated/uncensored** local model (qwen / the local mythos build) will
  actually attempt the exploit chain instead of refusing — appropriate for an
  *authorized* self-test. A larger model (Opus 4.8) can be the **planner/triage**
  brain that directs the smaller offensive executor and writes up findings.

## The loop
1. **Scope** — only targets in `scope.yaml`; default to an **isolated replica** of
   the stack (spun up by skbloom in a vcluster/throwaway env) so live prod isn't the
   test subject. Prod gets only safe, non-destructive checks.
2. **Recon → enumerate → attempt** — the model tool-calls a curated offensive toolset
   (nuclei templates, OWASP ZAP active scan, nmap, auth/secret-spray against our own
   endpoints) and reasons about chains the static scanners miss (logic flaws,
   chained misconfigs, exposed mythos/agent endpoints).
3. **Confirm** — a finding counts only with a reproducible PoC (no speculative noise);
   adversarially self-verified before it's filed.
4. **Feed the closed loop** — confirmed findings become `sksec/closed-loop` TRIAGE
   items → remediation PR → re-test (skred re-runs the same exploit to prove it's
   fixed). That's the *closed* in closed-loop.

## Guardrails (non-negotiable)
- **Scope-locked** to owned targets; refuses anything not in `scope.yaml`.
- **Non-destructive by default** against prod; destructive/chained attacks only in
  isolated replica envs with a kill-switch + auto-teardown.
- **Rate-limited + logged** — every action audited (so it's distinguishable from a
  real attacker and can't run away).
- **Findings stay internal** — fed to remediation, never disclosed externally.
- Human owns the go/stop; the offensive model has no merge/deploy rights — it only
  produces findings + PoCs.

This is the "hack our own envs to harden them" capability — real-time, continuous,
and wired straight to the patch loop so what it breaks gets fixed automatically.
