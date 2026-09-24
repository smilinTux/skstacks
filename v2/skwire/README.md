# skwire — the universal bootstrapper / wiring fabric

> *Terraform plans infrastructure. **skwire wires it** — secrets, connections, and
> confirmations — for humans and agents alike.*

skwire is the primitive underneath the SKStacks one-click experience. It turns a
set of **descriptors** (what each thing provides + needs) into a **Plan**, mints
every secret up front, injects them, pushes cross-service config, and brokers the
**"want me to just do it?"** handshake. Pure-stdlib core, zero deps — **embeddable
in any project**.

**Where skwire sits in the stack:** [../docs/STACK-MAP.md](../docs/STACK-MAP.md).

## Install & run

```bash
# one-liner (Linux/macOS): native binary → universal .pyz → pip, whichever fits
curl -fsSL https://github.com/smilinTux/skstacks/releases/latest/download/install.sh | sh
skwire                     # opens the chat-style setup; `skwire scan` just looks around
```

Three artifacts, every OS (built by CI per release tag — see
[`.github/workflows/skwire-release.yml`](../../.github/workflows/skwire-release.yml)):

| Artifact | Needs | Platforms |
|----------|-------|-----------|
| `skwire-linux-x64` / `skwire-macos-{arm64,x64}` / `skwire-windows-x64.exe` | nothing (native binary) | each built on its own OS |
| `skwire.pyz` (single file, ~150K) | Python 3.10+ | Linux **and** macOS **and** Windows |
| `pip install skwire` / `pipx` / `uv tool install` | Python 3.10+ | anywhere |

- **Windows**: `irm …/install.ps1 | iex`. skwire *runs* natively, but the things it
  *deploys* (Docker/Swarm, *arr media, OpenBao) target Linux/containers — deploy to
  WSL2 or a remote host (skwire asks "deploy here, or elsewhere?").
- **Build the single file yourself:** `bash build_pyz.sh` → `dist/skwire.pyz`.

## Three roles, one primitive
1. **Wiring engine** — *mint-then-inject*: generate every API key/secret first,
   inject pre-boot, then push cross-service config via each app's API. No more
   "boot → scrape a random key → paste it everywhere."
2. **Bootloader / bootstrapper** — like a stack initramfs: `seed → resolve → mint →
   bring-up → wire → handoff`, deterministic + resumable. Drop your code in (declare
   `provides`/`needs`) and skwire handles the rest.
3. **Confidence protocol** — a typed Plan + stable `plan_hash` that BOTH parties
   trust: **human↔AI** (you approve the hash; the AI can't deviate) and
   **service↔service** (each wire derives from the signed plan).

## Slick chat window (no terminal)
`skwire serve` opens a polished, **white-labelable** chat window in the browser —
no scary terminal. It greets you, asks to scan your env, shows the plan as a card,
and you click **"Heck yeah, do it! 🚀"**. Zero deps (stdlib http server + a single
HTML file); themed by the vanity layer (your name, logo, accent color). Brand it as
*DeployBot* — "powered by skwire" underneath.

## The UX it powers ("talk to your skos")
You talk → skos asks Socratic questions → skwire builds the Plan → the concierge
*says* it and asks **"want me to just do it?"** → you say **heck yeah** → it wires
everything up. The plain-English narration is `explain(plan)`; the "heck yeah" is
`approve(plan, approver)`, bound to the exact `plan_hash`.

```
I'll set up 4 services in order: jellyfin, prowlarr, sonarr, seerr.
I'll generate 3 secrets and wire 3 connections:
  • seerr → jellyfin (via jellyfin_api_key)
  • seerr → sonarr (via sonarr_api_key)
  • sonarr → prowlarr (via prowlarr_api_key)
Want me to just do it?
```

## Pack n ship (modularity — embed it in YOUR project)
A project bundles its skwire knowledge as a **Pack**: the descriptors it
contributes, project-specific **injectors**, extra **probes**, and its Socratic
**questions**. skwire merges all registered packs into one graph and configures
everything conversationally — **replacing hand-edited TUI/TOML config**.

```toml
# in YOUR project's pyproject.toml — installing the project registers its pack
[project.entry-points."skwire.packs"]
hermes = "hermes.skwire_pack:pack"          # a callable returning a Pack
```
```python
import skwire
skwire.load_packs_from_entrypoints()        # auto-discovers installed packs
plan = skwire.build_plan(skwire.all_nodes())
print(skwire.explain(plan))                 # "I'll set up …, wire …. Want me to do it?"
```
So `pip install hermes` → `skwire configure hermes` walks a non-technical user
through Hermes setup (providers/endpoints/keys) by asking questions — no TUI.
**skstacks** ships the `skstacks` pack; **Hermes** ships a `hermes` pack; anyone can.

### Extension points (the contracts you implement)
`SecretStore` (mint/get/put) · `Injector` (env/file/api `inject`) · `Signer`
(`sign` — capauth in prod) · `Probe` (env facts). All `runtime_checkable` Protocols
in `skwire.protocols` — the core depends only on these, never on a concrete impl.

## Catalog / app store (best-of-breed OSS, forkable)
skwire ships a **catalog** of offerings — clickable, standalone deployments of
best-of-breed open source (Media Server, Home Automation, AI Dev Toolchain,
Observability… even desktop apps like **GIMP**). The AI lists them ("what should I
install?") and recommends from what you say ("watch movies + edit photos" → GIMP +
Media Server). One behemoth installer that can set up *anything*.

```bash
skwire catalog                       # browse (or: skwire catalog "watch movies")
skwire add skmedia                   # one-click add → skwire up wires it in
```
Every offering is **open source with its repo listed** — so the loop is: install →
use → have the AI dev toolchain help you **contribute upstream or fork your own**.

The community catalog lives in **its own repo** ([`smilinTux/skwire-catalog`](https://github.com/smilinTux/skwire-catalog))
so adding an app is a PR to a JSON file — it never touches the engine. skwire
**auto-pulls** it from the raw URL (override with `SKWIRE_CATALOG_URL`), offline-safe.
Three open sources, no lock-in: first-party built-ins, the community catalog repo
(PR yours in), any `pip install`-able pack, or **host your own catalog**. Roll your own.

## A node is anything wireable
sk* services, 3rd-party apps (the *arr stack), **and the agent toolchain** — e.g.
install Claude Code / Codex / OpenCode and wire their endpoints + keys (OpenRouter/
OpenAI/Anthropic/local) into Hermes/OpenCode. An agent that "needs a model endpoint
+ key" is just another node; the key is a minted secret, the endpoint is wired config.

```python
from skwire.resolver import build_plan
from skwire.explain import explain
from skwire.approval import approve, verify_approval

plan = build_plan(nodes)            # nodes: [{name, provides{}, needs[]}]
print(explain(plan))                # the concierge speaks the plan
token = approve(plan, "chef")       # the "heck yeah" — bound to plan.plan_hash
assert verify_approval(plan, token) # executor refuses if the plan changed
```

## The full concierge arc
**preflight** (consent-gated scan → suggestions + "here or elsewhere?") → Socratic
intent → **resolver** (Plan + plan_hash) → **explain** ("want me to just do it?") →
**approval** (the "heck yeah", hash-bound) → mint → inject → wire → verify.

## Status
- ✅ `preflight` — consent-gated env probe (DI; `FakeProbe`/`SystemProbe`) + tailored
  `suggest()` (compute/model tier, orchestrator, exposure) + the deploy-where question.
- ✅ `resolver` — descriptors → graph → topo-order + edges + mints + stable `plan_hash`
  (cycle + missing-provider rejection). `models` — `WireEdge`, `Plan`.
- ✅ `explain` — Plan → plain-English narration (the bilingual interpreter).
- ✅ `approval` — `plan_hash`-bound approval token; pluggable signer (capauth in prod).
- ✅ `executor` (`mint`→`inject`→`wire`) — `execute(plan, token)` verifies approval,
  mints every secret (`RandomSecretStore`/your `SecretStore`), injects each into the
  provider + every consumer, masked report. Refuses unapproved plans.
- ✅ `rotate(plan, store, injector, keys=None)` — re-mint + re-inject a key (or all)
  **across the whole vertical in one call** (skwire owns the graph → no hunting down
  consumers). `skwire rotate [key]`.
- ✅ rotation **scheduling** — reads `rotation_days` off the descriptors, computes
  what's due, and *offers to schedule* (`offer_rotation_schedule` → cron). The chat
  window asks "want me to schedule these?" after wiring.
- ✅ CLI `skwire up` (plan→heck-yeah→wire) + `skwire rotate`.
- ⏳ next: real `Injector` impls (env/file/api) per app + the resumable bootloader
  stage-runner + the 4 descriptor fields on `app.yaml`.

**Design:** `docs/skhome-skmedia-design.md` (§3 `skos wire`) + `docs/skbloom-design-proposal.md`.
Embeddable: the core depends only on the stdlib; backends (secret store, signer,
injectors) are injected, so any project can `import skwire` and plug in its own.
