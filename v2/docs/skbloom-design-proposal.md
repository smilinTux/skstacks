> Part of the SKStacks v2 stack — see [STACK-MAP.md](STACK-MAP.md) for the full architecture map + how it all connects.

# skbloom — AI-First Sovereign SKStacks Installer

**Working name:** `skbloom` (the install/bloom experience) · `skinstaller` (the engine)
**Status:** Design proposal · **Date:** 2026-06-11 · **Author:** Lead architect
**Audience:** SKStacks v2 / skos / skguide maintainers

> One sentence: *Kubefirst's day-1 platform + Coolify's one-click ease + Plural's agent-ops — driven by a small self-hosted LLM, fully open (no Pro/BUSL gate), air-gappable, and adapter-swappable.* That exact combination is unclaimed in mid-2026.

---

## 1. Executive summary

**What we're building.** `skbloom` is an AI-first installer that stands up a complete, wired-together *sovereign* stack from one command (`skbloom up`) or a web UI — not a bare cluster, but Gateway-API/Traefik + ArgoCD app-of-apps + OpenBao/ESO secrets + a chosen family of `sk*` services, already SSO-linked. A small self-hostable LLM (pulled on install via Ollama) acts as the **concierge**: it gathers intent Socratically, translates it into a validated skos profile/descriptor, runs a dry-run, explains the plan, gates on human approval, then lets deterministic skos + ArgoCD code do every mutation. The same engine backs a one-click "install an sk\* stack" **app-store web UI**.

**Why it's differentiated.** The 2026 instant-IDP market has four layers — cluster lifecycle (CAPI/Talos/Crossplane), platform-in-a-box (Kubefirst/Plural/Qovery), package/app (Glasskube/Coolify/Dokploy), and developer-experience (Backstage/Port/Humanitec/Score) — and **nobody has unified them under one AI-first, fully-sovereign roof.** Every AI-native entrant in 2026 shares two weaknesses that define our opening:

1. **Cloud-LLM dependency.** OpenChoreo's MCP+SRE agent ([InfoQ](https://www.infoq.com/news/2026/04/openchoreo-10/)), Plural's Agent Runtime ([Plural Feb 2026](https://www.plural.sh/blog/february-product-update-2026/)), Coolify's MCP server — *all assume a hosted frontier model.* None pull a small self-hostable model at install time to drive the install itself. That space is **genuinely unoccupied**.
2. **Sovereignty gating.** The polished/AI/production experiences are paywalled or BUSL-locked: Kubefirst Pro (proprietary UI), Sidero Omni (BUSL — production = commercial, [Omni docs](https://docs.siderolabs.com/omni/overview/what-is-omni)), Humanitec Orchestrator (proprietary), Port (SaaS). The fully-open tier (CAPI, Crossplane, Glasskube, Kestra, Coolify) has **no AI concierge and no unified cross-layer installer.**

`skbloom` occupies exactly that white space: **air-gappable end-to-end (model + secrets + GitOps), adapter-swappable (our existing ports/adapters), and AI-augmented but never AI-dependent** (a deterministic non-AI path always exists). Our existing "no-catch-22 secrets bootstrap" (OpenBao/vault-file/SOPS-age/capauth + ESO) is *strictly better* than Kubefirst's Vault-only chicken-egg approach, and we already own most of the engine (skos + skguide). This is a thin AI-driver + web UI over assets that exist, not a new platform.

---

## 2. What to steal from Kubefirst — mapped to our v2 assets

Kubefirst is the most direct comparable and the UX bar to clear. The **architecture is the asset, not the roadmap** — upstream is mid-rebrand into Civo/Konstruct post-acquisition ([Civo acquires Kubefirst](https://www.civo.com/newsroom/civo-acquires-kubefirst)), so we fork the *flow*, not the trajectory. We keep the loved formula — *one command → fully-integrated platform + free local on-ramp + pre-wired SSO + one-click GitOps catalog + PR-based day-2* — and use the LLM to fix exactly what Kubefirst failed at (pre-flight, fail-fast, first-line support).

| Kubefirst pattern (verified source) | What it is | Our status | Net-new work |
|---|---|---|---|
| **Two-tier "tiny bootstrap cluster births the real cluster"** (`internal/launch/cmd.go`: downloads k3d/helm/mkcert, helm-installs console+API as `clusterType=bootstrap`) | A local k3d console cluster runs the API + UI that provisions the real cluster | **HAVE** k3d as a render target (`skos` renderers + v2 overlays) | Make the bootstrap cluster the **home of the Ollama model + skbloom web UI** — exactly where Kubefirst runs kubefirst-api+console. Maps 1:1. |
| **Ordered, idempotent, resumable provision watcher** (`provisionWatcher.go`: `InstallToolsCheck → DomainLivenessCheck → … → ArgoCDInstallCheck → VaultInitializedCheck → … → FinalCheck`, each writes a Viper flag, re-runs skip done work) | A named-step state machine; failed runs resume; reset+resume on error | **PARTIAL** `skos plan`/`install` split exists; no resumable named-step flags | Build a **named-step state machine** (`skbloom-steps.json` flag file) the LLM narrates and repairs *by step name*. This is the spine. |
| **"Everything is the gitops repo" + app-of-apps** (`registry/` = ArgoCD app-of-apps = desired state; add infra/app = open a PR) | The running cluster is a projection of one Git repo you own | **HAVE** ArgoCD app-of-apps + Kustomize overlays (dev/staging/prod) in v2 | Generate the customer's gitops repo from a template + token-fill (thin layer; prefer Kustomize overlays over raw Mustache where possible). |
| **PR-gated IaC apply** (Atlantis posts `plan` on PR, merge runs `apply` — full audit trail) | No local `terraform apply`; every Day-2 change is a reviewed PR | **PARTIAL** ArgoCD reconciles; no PR-gate on IaC | Add a PR-gated apply — but use **native CI (GitHub Actions / Forgejo Actions) or OpenTofu**, **not Atlantis** (showing its age; see anti-patterns). |
| **Vault as single OIDC/SSO + users-as-code** (`terraform/vault` + `terraform/users`; one Vault login → SSO to ArgoCD/Argo Workflows/console; IaC users auto-propagate) | One login, every tool; users + policies are reviewable code | **HAVE** OpenBao + capauth ("source of truth, dual-URI" identity layer) + `skvault` port | Steal the **users-as-code convention** + capture-init-output-into-k8s-secret pattern. Wire `sksso` (authentik/zitadel) + capauth as the single OIDC source. |
| **mkcert local-CA trick** (locally-trusted CA → real `*.kubefirst.dev` TLS in-browser, no LetsEncrypt) | Zero-friction local TLS for the free local install | **HAVE** `skca` (step-ca) port | Wire `skca`/mkcert into the local `skbloom up` path for instant TLS on `*.sk.local`. Big UX win, trivially portable. |
| **`metaphor` reference app** (a demo Go app exercising CI→ChartMuseum→ArgoCD→ingress→cert→dev/staging/prod) | A smoke test + teaching artifact in one | **PARTIAL** v2 has `apps/skai-platform`, `apps/skpayment` | Ship a canonical `sk-metaphor` stack ("Sovereign Chat" = skchat+skdata+skmodel+skfence+skvault) that exercises every rail — doubles as smoke test. |
| **Console GitOps Catalog** (one-click app install → writes PR/commit to gitops, ArgoCD reconciles; button-simple, audit-correct underneath) | One-click ≠ imperative; the click writes GitOps | **PARTIAL** skos `capabilities.yaml` + skguide recipes are the catalog source | Build the web UI catalog over `capabilities.yaml`/recipes; a click pre-fills the profile → plan → approve → PR/commit → ArgoCD. |

**What's heavy / DON'T copy blindly** (ports/adapters lets us make these opt-in, not baseline):
- **Atlantis + ChartMuseum + Crossplane + Argo Workflows + ESO + external-dns + reloader all at once** is right-sized for an enterprise mgmt cluster, heavy for a single-operator sovereign box. Make each a **swappable adapter**, not baseline.
- **Atlantis specifically** is dated — prefer OpenTofu + native PR-CI (`skinfra`=opentofu default).
- **ChartMuseum** is legacy (OCI registries / `helm push` superseded it) — skip.
- **Separate long-running `kubefirst-api` Go service** — for us the **LLM-driven installer is the orchestrator**; fold orchestration into the installer agent, no separate API service.
- **Mustache token-replacement** is fragile vs. a real engine — prefer Kustomize overlays/components we already have.

---

## 3. Architecture — the installer's components

The single most important safety property: **skos already separates "declare what" (descriptor + capability) from "resolve how" (profile + adapter).** The LLM only ever produces the *what* (a validated `app.yaml`/profile); deterministic, testable skos code owns the *how* (render + apply). The model never emits raw kubectl/tofu/compose.

```
┌──────────────────────────────────────────────────────────────────────┐
│  skbloom — runs IN the local k3d/Swarm bootstrap context              │
│                                                                        │
│  ┌─────────────┐   ┌──────────────────┐   ┌────────────────────────┐  │
│  │  skbloom    │   │  skbloom web UI  │   │  AI brain (2-model)     │  │
│  │  CLI        │   │  (app store +    │   │  • chat model (Socratic)│  │
│  │  `skbloom   │◄─►│   plan/diff/     │◄─►│  • tool model (descriptor│ │
│  │   up`       │   │   approve)       │   │    emit + decide)        │  │
│  └──────┬──────┘   └────────┬─────────┘   │  via Ollama /v1 (auto-   │  │
│         │                   │             │  pull), hosted/Claude    │  │
│         │   both surfaces   │             │  fallback adapter        │  │
│         └─────────┬─────────┘             └───────────┬──────────────┘  │
│                   ▼                                   │ (advise only)   │
│         ┌───────────────────────────────────────────┐│                 │
│         │  named-step state machine (skbloom-steps)  ││                 │
│         │  + MCP tool server (Tier 0/1/2, gated)     │◄┘                 │
│         └──────────────────┬────────────────────────┘                  │
│                            ▼  (deterministic, testable)                │
│   ┌──────────┐  ┌──────────────┐  ┌─────────────┐  ┌────────────────┐  │
│   │ skguide  │  │  skos        │  │ secrets     │  │ ArgoCD app-of- │  │
│   │ concierge│─►│ resolve/plan/│─►│ bootstrap   │─►│ apps reconcile │  │
│   │ + recipes│  │ render/      │  │ (OpenBao/   │  │ (the apply)    │  │
│   │          │  │ descriptor   │  │ SOPS/capauth│  │                │  │
│   └──────────┘  │ /install     │  │ +ESO)       │  └───────┬────────┘  │
│                 └──────────────┘  └─────────────┘          ▼           │
│                                              skmon/skpulse verify loop │
└──────────────────────────────────────────────────────────────────────┘
```

**Components:**

1. **CLI (`skbloom`)** — thin client over the engine; deterministic non-AI mode is the floor (`skbloom up --no-ai` runs scripted defaults). The CLI and web UI render the *same* objects (skguide's `Suggestion` is already a JSON dataclass; skos `plan`/`render` already emit data).

2. **Web UI** — two entry modes on one engine: **Browse** (app-store tiles from `capabilities.yaml`/recipes) and **Describe** (Socratic concierge). Both converge on the same `skos plan → diff → approve → ArgoCD apply` path. Lives in the bootstrap cluster; status tiles via `skpulse` (uptime-kuma) so the store doubles as the dashboard.

3. **AI brain (two-model split, matches our `skmodel`/`model_route`)** — a small **chat model** drives the Socratic interview turns; a stronger dense **tool model** does the constrained "emit + validate descriptor / decide next tool call" step. Auto-pulled via Ollama on install; hosted/Claude fallback as a swappable adapter (§5). **The LLM's only writes are: produce/refine a descriptor, and request approval.** Everything else is read-only or gated.

4. **skguide concierge + recipes** — already a runtime-agnostic intent router: `concierge.suggest(intent)` → ranked `Suggestion` (keyword match + mxbai embedding fallback), YAML recipe library (`intent_patterns → capabilities → plan_steps` w/ `invoke:` + `action_options`). We add `recipes/install_*.yaml` whose `plan_steps` reference skos verbs.

5. **skos engine** — `skos capabilities/resolve/plan/render/descriptor` (read-only) + `skos install` (apply). `descriptor` is the **schema wall**: every LLM-emitted `app.yaml` is validated before anything proceeds — invented ports/adapters are rejected (defends the #1 small-model failure: hallucinating a value not in the catalog).

6. **Secrets bootstrap** — our existing no-catch-22 plane (`skvault` = vault-file / OpenBao / capauth) + ESO. Captures init output into a k8s secret, the Kubefirst convention but without the Vault-only chicken-egg.

7. **ArgoCD app-of-apps** — the *only* apply mechanism for `team`/`enterprise`: the agent's "apply" = commit rendered manifests to the gitops repo + open a PR; ArgoCD reconciles. **The LLM never holds cluster credentials** — at most Git PR-open. For `personal`, apply can be a local `skos install` after explicit y/N.

**Tool surface + guardrails (enforced in the tool server, not the prompt):**

- **Tier 0 — read/plan (no gate):** `skguide.suggest`, `skos capabilities/resolve/plan/render/descriptor`, `argocd app diff/get`, `skmon/skpulse query`, `kubectl get/describe`.
- **Tier 1 — propose (draft, no infra change):** `emit_descriptor(app.yaml)` (validated), `git commit + open PR` (PR only, no merge).
- **Tier 2 — apply (gated):** `skos install`, `argocd app sync`, merge PR, `skstacks_secret_set`. Each requires a one-time **capauth-signed approval token scoped to a specific plan hash.**

Hard guardrails (defense in depth, prior art [containers/kubernetes-mcp-server](https://github.com/containers/kubernetes-mcp-server) `read-only`/`disable-destructive` modes; [MCP 2026 Roadmap](https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/)): default-deny / read-only by default; **no raw shell, no creds in-model** (named typed skos verbs only); **schema validation as a wall** (`skos descriptor`); allowlisted destructive ops with **re-type-the-resource-name** confirmation; **capauth-signed approval** (cryptographic, not a model claim); **plan-hash binding** (approve plan A, apply plan B is rejected — re-gate). This is the 2026-validated posture: plan-then-apply, PR-gated, human-in-the-pilot-seat ([Microsoft — Agentic Platform Engineering](https://devblogs.microsoft.com/all-things-azure/agentic-platform-engineering-with-github-copilot/), [Spacelift — Terraform AI](https://spacelift.io/blog/terraform-ai)).

> **Verified gap to close:** skos's README names `nomad` as a render target, but `src/skos/render/__init__.py` only wires `compose`/`swarm`/`kubernetes`. The roadmap (§7) must either ship the nomad renderer or drop the claim.

---

## 4. The AI-guided install flow + the app-store UI

### The pattern, end to end
```
intent (NL) → Socratic gather → draft app.yaml + profile → skos plan (dry-run)
  → render diff + sovereignty/cost summary → HUMAN APPROVAL GATE → skos install / PR
  → ArgoCD reconciles (app-of-apps) → skmon/skpulse verify → "here are your 3 next actions"
```
Every arrow that mutates state is a deterministic skos/ArgoCD op, never an LLM action.

### Step 0 — AI-driven pre-flight (fails BEFORE provisioning, not during)
Fixes Kubefirst's flagship pains (silent partial failure [issue #2138](https://github.com/kubefirst/kubefirst/issues/2138); mid-provision cloud/quota surprises). The concierge runs and *explains* a pre-flight: token scopes, quotas, region, DNS, disk/RAM (it knows this box is 16GB/no-GPU vs .100 has a 5060 Ti), **port conflicts** (e.g. 5432 vs skgentis on .41). Every blocker surfaces as plain-language remediation up front. **No run starts with a known-bad precondition.**

### Step 1 — Socratic requirement-gathering (three axes, slot-filling with confirmation)
Extend skguide's concierge into a typed `InstallProfile` slot-frame. Progressive disclosure — never dump the 25-capability catalog; ask one question, infer the rest from recipes, confirm.

| Axis | LLM asks | Slot filled |
|---|---|---|
| **What** | "What are you trying to run?" (chat stack? doc-search? full sovereign cloud?) | `capabilities[]` (via skguide recipe → skos catalog) |
| **Where** | "This laptop, a single VPS, or a cluster?" | `profile` ∈ {personal, team, enterprise}; `render_target` ∈ {compose, swarm, k8s/RKE2, k3d} |
| **How sovereign** | "Self-host everything, or allow Cloudflare/managed bits?" | adapter overrides (`skdns`=powerdns vs cloudflare; `skmesh`=netbird vs cloudflared); `skvault`=vault-file vs OpenBao vs capauth |

Defaults come from `skos resolve <cap> --profile personal`, so an under-specified answer still yields a **safe, sovereign-by-default** plan. **Sovereignty is the default; cloud is opt-in** — the inverse of Kubefirst's cloud-first framing. Git provider list includes **self-hosted Forgejo**, not just GitHub/GitLab.

### Step 2 — Intent → profile/plan (constrained, never free-text codegen)
1. **Intent → capabilities** via `skguide.concierge.suggest()`.
2. **Capabilities → descriptor:** LLM emits a candidate `app.yaml`, **validated by `skos descriptor`** (hard schema gate).
3. **Descriptor → plan:** `skos plan --profile <p>` resolves capability→adapter; `skos render` produces the manifest. Both **read-only**.

What the user sees is a **plan, not code:** *"You'll get matrix (skchat) + postgres (skdata) + ollama (skmodel) behind traefik (skfence), secrets in vault-file (skvault), on this laptop (personal/compose). 5 services, ~6 GB RAM. Sovereign: 100% local."*

### Step 3 — Plan-then-apply with approval gates
- **Diagnostic/plan phase is read-only** (Tier 0 only).
- **Render the diff, then gate:** show `skos render` / `argocd app diff` as the approval artifact. Risk-tiered gates: `personal`+compose on localhost = lightweight confirm; anything touching `enterprise`, secrets, or destructive ops = mandatory explicit approval with name re-type.
- **GitOps is the apply mechanism** for team/enterprise (commit + PR → ArgoCD reconciles); local `skos install` for personal.

### Step 4 — Rich, honest progress (fixes opaque ~35-min runs)
Live step-list ("VPC → cluster → ArgoCD → secrets bootstrap → sk\* apps → SSO wiring") with per-step status, elapsed/ETA, streamed logs behind a "details" toggle. **Never a spinner with no state.** Web UI and CLI show the same timeline (the named-step state machine from §2).

### Step 5 — Fail-fast, fail-loud, AI-diagnosed
Every step is a named idempotent unit with a hard gate. On failure: **stop immediately**, show which step + why, and have the LLM translate the raw error into cause + suggested fix + one-click **retry-from-here**. The model *is* the first-line support — directly answering Kubefirst's "limited community-only support" gap.

### Step 6 — Verify + finish-line handoff
Post-reconcile, query `skpulse`/`skmon`, report health; on drift, *propose* (not auto-apply) a corrective plan. End every run with the `kbot`-equivalent moment: store the admin credential **into OpenBao (not stdout-only)**, a single console URL, and an AI-generated **"here are your 3 next actions"** card.

### Clean teardown (fixes dirty-uninstall trust-poison)
`skbloom destroy` removes *everything* it created — cluster, volumes (explicit data-loss confirm for `skmem_pgdata`), **and** the generated gitops repo/branches — then **verifies no orphans remain**. The AI lists exactly what will be destroyed vs preserved before proceeding.

### The one-click "sk\* app store" web UI
- **Catalog = `capabilities.yaml` + skguide recipes as tiles** — no new catalog to maintain; same source of truth as the CLI. Each "app" is a stack descriptor (e.g. "Sovereign Chat" = skchat+skdata+skmodel+skfence+skvault).
- **Browse** → pick tile → pre-fills `InstallProfile` → plan/diff/approve gate.
- **Describe** ("a private ChatGPT for my family") → Socratic concierge builds the same `InstallProfile`.
- **One-click writes GitOps, never imperative kubectl** — the click commits a correct Kustomize overlay (dev/staging/prod) + lets ArgoCD reconcile. Button-simple on top, auditable underneath. UX bar: Cloudron / Coolify (280+ one-click apps); the differentiator is the **conversational driver in front of the catalog**.
- **Pre-wired SSO** — reuse capauth/OpenBao + `sksso` (authentik) as the single OIDC source: one login flows into ArgoCD, the sk\* console, skchat; IaC-added users auto-propagate.
- **Day-2 = same as day-0** — "add skchat to this deployment" → the AI drafts the overlay PR. One mental model end to end.
- **Multi-tenancy via vclusters as a first-class named feature** (gap Kubefirst left) — one-click per-agent/per-tenant vclusters with the AI explaining shared-node vs dedicated-node tradeoffs ([vCluster tenancy models](https://www.vcluster.com/guides/tenancy-models-with-vcluster)).

---

## 5. OOTB model recommendation + opt-in/fallback UX

**Gemini/"Gemini-4" is NOT self-hostable — excluded from OOTB; viable only as a hosted fallback.** Default OOTB pull targets the 1B–4B band (download must be <3 GB so it doesn't scare users, and must run CPU-only on hosts with no GPU — like this 16GB/no-GPU box).

### The hardware-detected ladder (auto-selected on install)

| Tier | Model | Pull (Q4) | RAM/VRAM | Auto-selected when | License |
|---|---|---|---|---|---|
| **Floor** | `llama3.2:3b` or `qwen3:1.7b` | ~2 GB | 4 GB RAM, CPU OK | <8 GB RAM, no GPU | Llama community / Apache-2.0 |
| **Default** | **`qwen3:4b`** | ~2.5 GB | 6–8 GB RAM or ~3 GB VRAM | 8–16 GB RAM / any modern laptop | **Apache-2.0** |
| **Quality** | `qwen3:8b` or `gemma3:12b` | ~5–8 GB | 12–16 GB VRAM | discrete GPU ≥12 GB | Apache-2.0 / Gemma |
| **Specialist** | `xLAM-2-3b-fc-r` | ~2 GB | 4 GB | max single-turn tool accuracy on tiny HW | — |
| **Fallback** | hosted (Claude / OpenAI-compatible) | 0 (network) | none | user opts out of local, or has API key | — |

**Why `qwen3:4b` as default:** best tool-calling-per-byte in 2026 — BFCL Qwen3-4B 62% overall / 75.5% live / 82.6% non-live AST ([BFCL](https://gorilla.cs.berkeley.edu/leaderboard.html), [llm-stats](https://llm-stats.com/benchmarks/bfcl)); Apache-2.0 (redistributable inside an installer image); runs CPU-only on 8 GB ([Qwen3-4B VRAM](https://willitrunai.com/blog/qwen-3-gpu-requirements)). `xLAM-2-3b-fc-r` is the strongest tiny tool-caller (65.7% overall / 81% live) — ship as a specialist option.

**Honesty about small-model limits (load-bearing for the architecture):** multi-turn tool-calling **collapses below ~4B** (Qwen3-4B 35% multi-turn, 1.7B 17%, 0.6B ~1%). **Therefore: never ask a 4B model to autonomously drive a long multi-turn agent loop.** Each LLM step is a **fresh, single-turn, schema-constrained tool call**: (1) JSON-schema/GBNF grammar-constrained decoding (Ollama structured outputs / llama.cpp), (2) narrow tool surface per step (3–6 tools — small-model accuracy degrades sharply with tool-list length, [arxiv 2411.15399](https://arxiv.org/pdf/2411.15399)), (3) the model does intent→next-action + param-fill; **our deterministic state machine owns orchestration.** This is why the two-model split in §3 works: dense beats MoE for agentic tool-calling, and we already run qwen3.6-27b-abliterated on .100:8082 + mxbai-embed on Ollama for the strong-model role.

### The Ollama auto-pull UX
- **Auto-pull with a real progress bar:** `POST localhost:11434/api/pull {"model":"qwen3:4b","stream":true}` streams `status`/`total`/`completed` bytes → web progress bar; **pulls are resumable** (flaky networks recover) ([Ollama pull API](https://docs.ollama.com/api/pull)).
- **Inference via OpenAI-compatible `/v1`:** `base_url=http://localhost:11434/v1` — same surface a hosted provider exposes, so local↔hosted is a **one-line config swap** ([Ollama OpenAI compat](https://docs.ollama.com/api/openai-compatibility)).

**First-run flow:**
1. "Enable AI-guided setup?" → **No** falls through to full manual/scripted install (no model, fully usable — AI augments, never blocks).
2. **Yes** → detect HW → pick tier → "Will download Qwen3-4B (~2.5 GB) to drive guided install. [Use local] / [Use my API key] / [Skip]".
3. Stream pull with progress; **cache to the platform model volume** so re-installs / air-gapped clones don't re-download.
4. Pre-warm one `/v1` call → "model ready."

**Fallback / sovereignty:** the LLM is a **swappable adapter** (one port, three backends — local Ollama default / hosted OpenAI-compatible / Claude via a thin OpenAI-shape translator). **Default to local: the OOTB path is offline and sovereign** at $0/token. **Air-gap:** point the pull at a mirror / pre-seeded model volume, or `ollama create` from a local GGUF baked into an optional "offline bundle." **License hygiene:** bundle only **Apache-2.0** weights (Qwen3, SmolLM3); treat Gemma (use-policy addendum) and Llama (community license) as opt-in pulls, not baked-in defaults. **No telemetry, no required account** in the local path.

---

## 6. Business-model lessons from Kubefirst (sustain without selling out the OSS core)

Kubefirst's CEO John Dietz was unusually candid that **their own architecture fought monetization**: a platform built for *complete user independence and zero lock-in* is *"very, very difficult to commercialize"* — the OSS product was so complete there was little a customer *needed* to pay for. Resolution: **monetize convenience (a UI layer + lifecycle automation + support), not capability** — deliberately *not* crippling the core. The path ended in acquisition by Civo (a cloud provider that monetizes the *layer underneath* — compute — and can keep the IDP free as a funnel), not independent venture growth ([the story behind Konstruct](https://www.civo.com/blog/the-story-behind-konstruct), [Kubelist podcast](https://www.heavybit.com/library/podcasts/the-kubelist-podcast/ep-46-kubefirst-with-john-dietz-of-konstruct)).

**Concrete lessons for skbloom/skstacks:**
1. **Sell convenience, not capability.** Keep the OSS core complete and lock-in-free (the LLM-driven guided deploy + "install an sk\* stack" must be fully functional offline). The wedge is a **hosted control-plane / fleet-management UI / paid support** — never gated core features. (Kubefirst gated the *UI*, not the engine — the good model.)
2. **A zero-lock-in architecture is a monetization headwind — design the wedge up front.** Kubefirst learned this late. Decide early what someone pays for when the product keeps nothing hostage: hosted convenience, **multi-tenant fleet ops** (where "single-team magic" breaks — Civo framed the hard problem as repeating one team's GitOps across 50 teams), compliance/SBOM, support contracts.
3. **A strategic adjacency funds the commons better than VC.** Tie skbloom's sustainability to something we already monetize/control (**hosting, hardware, a managed sovereign-cloud offering**) rather than extracting rent from the installer itself.
4. **Beware the open-core trap that sells out the core.** If we take outside money, structure it so growth expectations can't force capability behind the wall (the relicense/acquisition pressure that hits VC-backed open-core).
5. **Build on, and credit, the OSS commons** (Argo, Ollama, OpenTofu, Tinkerbell/CNCF) — legitimacy and adoption come from being a good upstream citizen. Dietz: *"We're only where we are because companies like GitLab offered free software."*

> Civo's upstream is **alive but absorbed** (v2.10.5, Jan 31 2026; commits into June 2026 — no sunset), underwritten by Civo's cloud revenue, not Kubefirst Pro standing alone. The thing people loved is being folded into a proprietary IDP — **a real opening for a sovereign successor.**

---

## 7. Phased build roadmap (MVP → full)

Each phase maps to concrete repos/tasks. Repos: extend **`skbloom`** (new), **`skos`**, **`skguide`**, **`SKStacks/v2`**.

### Phase 0 — Foundations & gap-closing (prereq)
- **`skos`:** ship the **nomad renderer** or drop the README claim (verified gap: `RENDERERS` only wires compose/swarm/kubernetes). Add **k3d/RKE2 render targets** as first-class.
- **`skos`:** make `skos plan`/`render` emit a stable **plan-hash** + machine-readable JSON (the approval-token binding from §3).
- **Coord epic:** `skbloom-mvp`. Tasks: `T0.1 nomad-renderer`, `T0.2 plan-hash`, `T0.3 k3d/RKE2 targets`.

### Phase 1 — MVP: `skbloom up` CLI, deterministic, local-only (NO AI yet)
The fail-safe floor. Proves the engine before adding the brain.
- **New repo `skbloom`:** the named-step state machine (`skbloom-steps.json` flag file, resumable, reset+resume) + CLI `skbloom up/status/destroy`.
- **Wires:** k3d/Swarm bootstrap (local), mkcert/`skca` local TLS, secrets bootstrap (`skvault`=vault-file), ArgoCD app-of-apps install, one canonical stack (`sk-metaphor` = "Sovereign Chat").
- **Clean teardown** with orphan verification.
- **Deliverable:** `skbloom up` → working local sovereign Sovereign-Chat stack on this box, no AI, no cloud. Tasks: `T1.1 state-machine`, `T1.2 bootstrap-cluster`, `T1.3 sk-metaphor-stack`, `T1.4 teardown+verify`.

### Phase 2 — AI concierge (advise-only, single-turn, schema-gated)
- **`skguide`:** add `recipes/install_*.yaml` (plan_steps reference `skos plan/render/install` + ArgoCD); extend `concierge.suggest` into the typed `InstallProfile` slot-frame + Socratic interview.
- **`skbloom`:** Ollama auto-pull (HW-detected ladder, resumable, progress stream); LLM adapter port (local/hosted/Claude); two-model split; **pre-flight validator** (tokens/quota/DNS/RAM/port-conflicts).
- **Guardrail:** `skos descriptor` schema-wall enforced; LLM emits descriptors only.
- **Deliverable:** `skbloom up --ai` → Socratic intent → plan → diff → y/N → apply, with AI fail-fast diagnosis. Tasks: `T2.1 install-recipes`, `T2.2 InstallProfile+interview`, `T2.3 ollama-autopull`, `T2.4 llm-adapter`, `T2.5 preflight`.

### Phase 3 — Approval gates + GitOps PR-apply + MCP tool server
- **`skbloom`:** MCP tool server (Tier 0/1/2); **capauth-signed approval tokens** bound to plan-hash; PR-gated apply (commit + PR to gitops, native CI/Forgejo Actions, **not Atlantis**); ArgoCD reconcile.
- **`skos`:** `terraform/tofu users-as-code` convention; capture-init-secret-into-k8s.
- **Deliverable:** team/enterprise profile — apply = reviewed PR, ArgoCD reconciles, LLM holds only PR-open. Tasks: `T3.1 mcp-server+tiers`, `T3.2 approval-token`, `T3.3 pr-gated-apply`, `T3.4 users-as-code`.

### Phase 4 — The app-store web UI + SSO + verify loop
- **New `skbloom-ui`:** catalog tiles over `capabilities.yaml`/recipes; Browse + Describe modes; live `skpulse`/`skmon` status tiles; same plan/diff/approve affordances as CLI.
- **`sksso`:** wire authentik/zitadel + capauth as single OIDC; IaC-user auto-propagation.
- **Finish-line:** admin cred → OpenBao, console URL, "3 next actions" card.
- **Deliverable:** one-click "install an sk\* stack" web UI + pre-wired SSO. Tasks: `T4.1 catalog-ui`, `T4.2 describe-mode`, `T4.3 sso-wiring`, `T4.4 status-tiles`.

### Phase 5 — Full: agent-ops, multi-tenancy, air-gap bundle, cloud targets
- **Agent-ops** (Plural/OpenChoreo-style on the *local* model): upgrade-agent (drafts reviewable PRs), CVE-remediation, SRE root-cause via `skmon` — all advise+PR, never auto-apply.
- **Multi-tenancy:** one-click per-agent/per-tenant **vclusters**.
- **Air-gap:** offline model bundle (`ollama create` from baked GGUF) + mirror-pull.
- **Cloud render targets** (CAPI/Talos substrate) as opt-in adapters — sovereignty stays default.
- **Sustainability wedge:** hosted fleet-management control-plane (the paid layer; core stays free).
- Tasks: `T5.1 upgrade+cve-agents`, `T5.2 vcluster-tenancy`, `T5.3 airgap-bundle`, `T5.4 capi-talos-targets`, `T5.5 hosted-fleet-cp`.

---

## Key file references
- `v2/{apps,overlays}` in this repository: the app-of-apps and Kustomize overlay
  layout the renderer targets.
- The renderer, catalog and concierge referenced above live in separate
  SKWorld projects outside this repository.
