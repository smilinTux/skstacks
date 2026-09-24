# SKStacks v2 — Stack Map & Design Decisions (2026-06-11)

A single map of everything: the validated best-of-breed stack, the AI-first
installer (**skwire** + skbloom), and how all the components connect. Companion to
`2026-06-11-skstacks-v2-stack-validation.md` (the best-of-breed audit) and
`skbloom-design-proposal.md` / `skhome-skmedia-design.md` (the AI-installer + ports).

---

## 1. The layers — where everything sits

```mermaid
flowchart TD
    U([Operator / AI agent]) -->|talks to| BLOOM
    subgraph INSTALL["AI-first installer"]
      BLOOM["skbloom\n(AI concierge: Socratic intent → plan)"]
      WIRE["skwire\n(universal bootstrapper / wiring fabric)"]
      BLOOM --> WIRE
    end
    WIRE -->|drives| OS
    subgraph OS["skos — sovereign agent OS (ports/adapters, 4C)"]
      direction LR
      CLOUD["cloud/\nskfence skmesh skdns skcicd skinfra skdweb"]
      COMMS["comms/\nskcomms skchat skvoice skbus"]
      COMPUTE["compute/\nskdata skcache skobject skblock skfile\nskmodel skflow skmon skpulse skbackup"]
      CORE["core/\ncapauth sksso sksec skwaf skca skvault"]
    end
    OS -->|renders to| PLAT
    subgraph PLAT["platform targets (same descriptors → many targets)"]
      SWARM["Docker Swarm"]
      K8S["Kubernetes / RKE2"]
      K3D["k3d (local)"]
    end
    CORE -.secrets.-> VAULT["OpenBao (HA Raft) /\nvault-file / SOPS-age / capauth"]
    CLOUD -.ingress.-> GW["Traefik Gateway API\n(+ exposure: lan/tailscale/cloudflared/netbird/pangolin)"]
    WIRE -.pulls.-> CAT["skwire-catalog repo\n(best-of-breed OSS offerings)"]
```

**Key idea:** one set of `app.yaml` descriptors (each declares `provides` / `needs`
/ `secrets` / `config`) is the single source of truth. skos resolves them to a
platform; **skwire** wires them together (secrets + connections); skbloom puts an AI
concierge in front.

---

## 2. skwire — the universal bootstrapper

The engine under the one-click experience. Pure-stdlib, zero-dep, embeddable.

```mermaid
flowchart LR
    subgraph ENGINE["skwire engine (import skwire)"]
      PRE["preflight\nscan env + suggest\n(here or elsewhere?)"]
      RES["resolver\ndescriptors → graph\n→ Plan + plan_hash"]
      EXP["explain\nPlan → plain English\n(want me to just do it?)"]
      APP["approval\n'heck yeah' →\nhash-bound token"]
      EXEC["executor\nmint → inject → wire"]
      ROT["rotation\nre-mint + re-inject\nacross the vertical"]
      RED["redundancy\n'if you need one,\nget two'"]
      PRE --> RES --> EXP --> APP --> EXEC --> ROT
      RES --> RED
    end
    subgraph EXT["extension points (Protocols)"]
      SS["SecretStore\nmint/get/put"]
      IN["Injector\nenv/file/api"]
      SG["Signer\ncapauth/PGP"]
      PB["Probe\nenv facts"]
    end
    EXEC -.uses.-> SS & IN
    APP -.uses.-> SG
    PRE -.uses.-> PB
    subgraph SHIP["surfaces"]
      CLI["CLI: scan/plan/up/rotate/\ncatalog/add/serve"]
      WEBUI["chat window\n(skwire serve)"]
      PACK["packs\n(pip + entry-points)"]
      CATLG["catalog\n(builtin + remote repo)"]
      BRAND["vanity / white-label"]
    end
    SHIP --> ENGINE
```

**The arc (what a user experiences):**

```mermaid
sequenceDiagram
    participant U as User
    participant C as Chat window (branded)
    participant E as skwire engine
    participant X as executor
    U->>C: opens skwire serve
    C->>E: scan env (with consent)
    E-->>C: suggestions + "here or elsewhere?"
    C->>E: browse catalog / pick offerings
    E-->>C: Plan ("set up X, wire Y") + redundancy advice
    C->>U: "want me to just do it?"
    U->>C: Heck yeah 🚀
    C->>X: approve(plan_hash) → execute
    X->>X: mint every secret → inject provider+consumers → wire
    X-->>C: done; "auto-rotate every 90d?"
```

---

## 3. OOTB auto-wiring — mint-then-inject (the moat)

The thing nobody else does: generate every secret **first**, inject pre-boot, push
cross-service config — so the stack wires itself with zero manual steps. And because
skwire owns the graph, **rotating one key re-wires it everywhere in one call.**

```mermaid
flowchart TD
    D["descriptors\nprovides / needs / secrets"] --> G["wire graph\n(topo-sorted)"]
    G --> M["MINT all secrets up front\n(SecretStore: OpenBao/capauth/vault-file)"]
    M --> I["INJECT into provider + every consumer\n(Injector: env / file / app-API)"]
    I --> V["verify + report (masked)"]
    G -. rotation .-> R["rotate(key) →\nre-mint + re-inject\nALL who touch it"]
    R -. schedule .-> S["rotation_days → cron\n'want me to schedule?'"]
```

---

## 4. Catalog / marketplace — open at every edge

```mermaid
flowchart LR
    SKW["skwire\nload_catalog()"] --> B["built-in offerings\n(first-party)"]
    SKW -->|auto-pull| REPO["skwire-catalog repo\ncatalog.json (PRs)"]
    SKW --> PIP["pip-installed packs\n(skwire.packs entry-point)"]
    SKW --> OWN["your own catalog\n(SKWIRE_CATALOG_URL)"]
    REPO -.community PRs.-> REPO
    classDef oss fill:#16323a,stroke:#22d3ee;
    class B,REPO,PIP,OWN oss
```

All offerings are **best-of-breed OSS** (each ships its license + repo). The loop:
*install → use → AI helps you contribute upstream or fork your own.* Take the engine
and roll your own — no lock-in.

---

## 5. Where it all connects (repos, sync, site)

```mermaid
flowchart TD
    DEV["build host\nbuild here"] -->|push| GH["github: smilinTux/skstacks (main)\nv2/skwire = the engine"]
    GH -->|ff-merge| P41["primary dev host\n(always current)"]
    GH -. pins a tag .-> PRIV["private instance repo\n(see docs/INSTANCE-MODEL.md)"]
    SKW2["smilinTux/skwire-catalog\n(community offerings)"] -->|raw.githubusercontent| SKWENG["skwire auto-pull"]
    SITE["smilinTux/skwire-skworld-io\n→ skwire.skworld.io (CF→GH-Pages)"]
    GH --- SITE
```

---

## 6. Design decisions (the load-bearing ones)

| Decision | Why |
|---|---|
| **OpenBao default secret server** (not HashiCorp Vault) | LF/MPL-2.0, wire-compatible, free Namespaces; avoids BSL. |
| **No-catch-22 secret bootstrap** | vault-file/capauth are server-less roots → bootstrap OpenBao with PGP-encrypted init (no plaintext); K8s-auth = no pre-shared secret. The factory default stays `vault-file`. |
| **Gateway API + Traefik** (not ingress-nginx) | ingress-nginx EOL Mar-2026; consolidate on the skfence edge engine — one ingress stack. |
| **Two exposure ladders** | mesh: Tailscale(ease)→Netbird(sovereign); tunnel: cloudflared(ease)→Pangolin(sovereign); `lan` always-on baseline kills the bootstrap catch-22. |
| **Loki→VictoriaLogs, Uptime-Kuma→Gatus, +Velero, +Tetragon/Kyverno** | license + footprint + closing real gaps (K8s backup, enforcement, admission). |
| **skwire = mint-then-inject + signed plan handshake** | the OOTB-wiring moat; the plan_hash is the trust anchor for human↔AI and service↔service. |
| **Catalog in its own repo** | adding an app is a PR to JSON — never touches the engine; easy governance. |
| **"if you need one, get two"** | redundancy advisor surfaces load-bearing services and offers an HA pair, in the install flow. |
| **Closed-loop security + authorized self-red-team** | nightly scanners → AI-triage → PR (never auto-merge) → deploy; skred attacks own scope-locked envs to harden. |

---

## 7. Status snapshot (2026-06-11)

- **Platform/secrets/storage/observability/exposure** — built across `v2/` (~190 tests).
- **skwire** — complete shippable product: engine + executor + rotation + catalog +
  redundancy + chat window + vanity + docs + installer + CLI (74 tests, zero-dep).
- **Live:** `skwire-catalog` repo (auto-pulled), `skwire.skworld.io` site.
- **Next:** OpenBao redundancy/HA verification + skwire "deploy a pair", real per-app
  Injectors, flesh skmedia/skhome packs, skos nomad-renderer fix, skred orchestrator.
