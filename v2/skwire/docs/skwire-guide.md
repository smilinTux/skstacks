# skwire Guide — build on it (for AIs and humans)

> A complete, copy-pasteable guide. If you're an AI agent adding skwire to a
> project: read this top-to-bottom, then copy the **Build-Your-Own-Pack recipe**
> and swap in your project's nodes/injector. Everything here runs as-is.

---

## 1. What skwire is (one paragraph)

skwire turns **descriptors** (what each thing *provides* + *needs*) into a **Plan**,
mints every secret up front, injects them, and pushes cross-service config — so a
stack wires *itself* with zero manual config. It also narrates the plan in plain
English and brokers a hash-bound **"heck yeah"** approval. Pure-stdlib core, zero
runtime deps, cross-platform (Linux/macOS/Windows), **embeddable in any project**.

The arc:
```
preflight → (Socratic intent) → resolver → explain → approval → mint → inject → wire → verify
  scan+ask       LLM fills in     graph→Plan  narrate   "heck yeah"   └──── executor (next) ────┘
```

## 2. Install (cross-platform, all-in-one)

```bash
# Linux / macOS
./install.sh
# Windows / anywhere (pure-Python bootstrap)
python install.py
# or directly (zero deps):
pip install skwire        # once published; or `pip install .` from a checkout
```
Then: `skwire scan` · `skwire packs` · `skwire plan`.

## 3. Core concepts

- **Node** — anything wireable: an sk* service, a 3rd-party app (Sonarr), OR an
  agent-toolchain component (OpenCode needing an OpenRouter endpoint+key). Shape:
  ```python
  {"name": "sonarr",
   "provides": {"url": "http://sonarr:8989", "api_kind": "servarr"},  # optional
   "needs": [{"service": "prowlarr", "secret": "prowlarr_api_key"}]}  # optional
  ```
- **Plan** — `order` (bring-up order, providers first), `edges` (consumer→provider
  via secret), `mints` (secrets to generate), `plan_hash` (stable, order-independent
  — the trust anchor).
- **Pack** — what a project ships: its nodes + project-specific injectors + extra
  probes + Socratic questions. Discovered via the `skwire.packs` entry point.
- **Extension points** (Protocols in `skwire.protocols`) — `SecretStore`,
  `Injector`, `Signer`, `Probe`. The core depends only on these.

## 4. Quickstart (60 seconds)

```python
import skwire

nodes = [
    {"name": "prowlarr", "provides": {"url": "http://prowlarr:9696"}},
    {"name": "sonarr", "provides": {"url": "http://sonarr:8989"},
     "needs": [{"service": "prowlarr", "secret": "prowlarr_api_key"}]},
    {"name": "seerr", "needs": [{"service": "sonarr", "secret": "sonarr_api_key"}]},
]

plan = skwire.build_plan(nodes)
print(skwire.explain(plan))             # "I'll set up 3 services… want me to just do it?"
token = skwire.approve(plan, "you")     # the "heck yeah" — bound to plan.plan_hash
assert skwire.verify_approval(plan, token)
```

## 5. Build-Your-Own-Pack recipe (the main event)

A Pack is how YOUR project plugs in. Five steps:

### Step 1 — declare your nodes
What does each piece *provide* (URL/API) and *need* (a dependency + the secret)?

### Step 2 — implement an Injector (how config reaches your app)
```python
class MyInjector:
    method = "api"                       # "env" | "file" | "api"
    def inject(self, target: str, key: str, value: str) -> bool:
        # e.g. POST value to target's config API, or write its .env, etc.
        ...
        return True
```

### Step 3 — (optional) implement a SecretStore (where minted secrets live)
```python
class MyStore:
    def mint(self, key): ...   # generate + persist a new secret
    def get(self, key): ...
    def put(self, key, value): ...
```

### Step 4 — assemble the Pack
```python
from skwire import Pack

def pack() -> Pack:
    return Pack(
        name="myproject",
        nodes=[
            {"name": "backend", "provides": {"url": "http://backend:9000"}},
            {"name": "frontend", "needs": [{"service": "backend", "secret": "backend_api_key"}]},
        ],
        injectors={"api": MyInjector()},
        questions=["Which region? (us / eu)"],
    )
```

### Step 5 — ship it (auto-discovery)
In your project's `pyproject.toml`:
```toml
[project.entry-points."skwire.packs"]
myproject = "myproject.skwire_pack:pack"
```
Now `pip install myproject` makes it discoverable:
```python
import skwire
skwire.load_packs_from_entrypoints()     # finds installed packs
print(skwire.explain(skwire.build_plan(skwire.all_nodes())))
```
A non-technical user runs `skwire plan` and gets a conversational setup — **no TUI**.

> A complete working pack is in `examples/hello_pack/` — copy it and rename.

## 6. Extension points (full contracts)

All are `runtime_checkable` Protocols — implement the methods/attrs, that's it:

| Protocol | You implement | Used for |
|---|---|---|
| `SecretStore` | `mint(key)`, `get(key)`, `put(key, value)` | where minted secrets live (vault/OpenBao/capauth/env) |
| `Injector` | `method: str`, `inject(target, key, value) -> bool` | env-override / config-file / app-API push |
| `Signer` | `sign(payload) -> str` | sign the approval (capauth/PGP in prod) |
| `Probe` | env-fact attributes (see `preflight.SystemProbe`) | custom environment scanning |

Plug a signer into approval:
```python
def capauth_signer(payload: str) -> str:
    return capauth.sign(payload)         # your impl
token = skwire.approve(plan, "you", signer=capauth_signer)
```

## 7. The CLI

```
skwire scan            # probe env + suggestions ("deploy here or elsewhere?")
skwire packs           # list discovered/registered packs
skwire plan            # build + narrate the wiring plan
skwire up              # plan → "heck yeah" → mint → inject → wire (the whole thing)
skwire rotate [key]    # re-mint + re-inject a key (or all) across the vertical
skwire catalog [query] # browse best-of-breed OSS offerings (AI picks from `query`)
skwire add <name>      # add an offering → `skwire up` wires it in
skwire serve           # the slick branded chat window (no terminal)
```

## 7b. Execute, rotate, schedule

```python
import skwire
plan  = skwire.build_plan(nodes)
token = skwire.approve(plan, "you")                       # the "heck yeah"
res   = skwire.execute(plan, token)                       # mint → inject → wire
skwire.rotate(plan, store, injector, ["api_key"])         # re-wire one key everywhere
skwire.offer_rotation_schedule(nodes)                     # "auto-rotate every 90d?"
```
`execute` refuses any plan that doesn't match the approval. `rotate` re-mints a key
and re-injects it into its provider + every consumer in one call (skwire owns the
graph). Rotation policy comes from each secret's `rotation_days` on the descriptor.

## 7c. Catalog (app store)

```python
skwire.load_catalog()                  # built-ins + the community repo (auto-pulled)
skwire.recommend("watch movies")       # AI ranks offerings to intent → [skmedia, …]
skwire.install_offering("skmedia")     # add it to the graph
```
Offerings are best-of-breed OSS (each ships its license + repo). The community
catalog is a separate repo (`smilinTux/skwire-catalog`) — add yours via a PR to a
JSON file, host your own (`SKWIRE_CATALOG_URL`), or `pip`-publish a pack.

## 8. Cross-platform notes

- Core is pure stdlib → runs anywhere Python 3.10+ runs (Linux/macOS/Windows).
- `SystemProbe` is cross-platform: RAM via POSIX `sysconf` *or* the Windows
  `GlobalMemoryStatusEx`; disk via the platform-appropriate root (`/` vs `C:\`).
- No network calls happen in a probe without consent; no compilers/system packages
  needed to install.

## 9. API reference (public surface)

```python
from skwire import (
    probe_env, suggest, EnvProfile, Suggestion, FakeProbe, SystemProbe,  # preflight
    build_plan, Plan, WireEdge, WireCycleError, MissingProviderError,    # resolver
    explain,                                                             # narrate
    approve, verify_approval, ApprovalToken,                             # approval
    execute, rotate, ExecutionResult,                                   # executor
    RandomSecretStore, mint_secrets, RecordingInjector,                # mint/inject
    rotation_schedule, due_for_rotation, cron_for, offer_rotation_schedule,  # rotation
    Pack, register_pack, get_pack, list_packs, all_nodes,                # packs
    load_packs_from_entrypoints, clear_registry,
    Offering, list_offerings, install_offering, recommend,             # catalog
    load_catalog, load_remote_catalog, load_catalog_file,
    Branding, set_branding, active_branding, banner,                   # vanity
    SecretStore, Injector, Signer, Probe,                               # contracts
    __version__,
)
```

## 10. Embedding checklist (for an AI adding skwire to a project)

1. `pip install skwire` (zero deps).
2. Write `yourproject/skwire_pack.py` with a `pack()` returning a `Pack` (§5).
3. Add the `skwire.packs` entry point to your `pyproject.toml`.
4. Implement an `Injector` for how your app takes config; optionally a `SecretStore`.
5. `skwire plan` to verify the narration; wire your "approve" UX to `approve()`.
6. Done — your project now configures itself conversationally and is wire-graph-aware.
