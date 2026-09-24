# skbloom Web UI — 2027 control-plane design

Design reference for the skbloom app-store / quasi-control-plane UI. Single-file,
zero-build, vanilla HTML/CSS/JS over a small JSON+SSE API. Implemented in
[`../skbloom/web/`](../skbloom/web/).

## Landscape verdicts (what to steal / avoid)
- **Emulate:** Dokploy (one clean intentional path, single env surface), **Glasskube**
  (typesafe *form-from-schema* — maps directly onto our `app.yaml` `config:` block),
  Kubefirst console (provisioning timeline).
- **Avoid:** Coolify tab-sprawl, Portainer/Rancher/CapRover Bootstrap-era density,
  raw-YAML-first, "every knob visible at once". Yacht is dead.
- **The tell of "dated":** all options exposed simultaneously, no progressive disclosure.

## "2027 feel" (key correction from research)
> **Glassmorphism is now flagged as dated/overused.** The live direction is **calm
> interfaces + transparent AI + functional (not decorative) motion.** Flat-with-depth-cues
> (1px hairline borders + one soft shadow), NOT frosted glass.

Implemented patterns (all vanilla):
- **Design tokens** (CSS custom properties): dark near-black canvas, one elevated surface,
  one accent (teal), generous whitespace, system font. Theming = swap the token block.
- **⌘K / Ctrl-K command palette** — the single biggest 2027 tell; fuzzy over apps + actions.
- **Optimistic / streaming UI** — SSE install stream; steps render immediately then flip
  pending→running→ok/err via a status dot.
- **Living status tiles** with health dots (poll `/api/status` every 5s), **skeleton
  loaders** while fetching.
- **Functional micro-interactions** only (dot cross-fade, row rise); honors
  `prefers-reduced-motion`; `aria-live` on the install stream.

## Quasi-control-plane scope — the keep-it-simple line
Mapped field-by-field onto the `app.yaml` descriptor:

| Descriptor field | Surface | Control |
|---|---|---|
| `config:` (LOG_LEVEL, RATE_LIMIT_*…) | **SHOW** | form-from-descriptor: enum→select, int→number, else text; defaults pre-filled |
| `secrets:` (key names) | **SHOW** (display) | listed as "set at deploy"; values never sent to client |
| `ha` / `min_replicas` | **SHOW** | a single replica stepper |
| `depends_on` | show read-only chips | transparency, not editable |
| `deploy` (image/ports/volumes) | **Advanced ▾** | read-only summary by default |
| `healthcheck` | **Advanced ▾** | display only |
| volumes / rollback / authoring / metrics / exec | **CLI only** | stating this boundary *is* the simplicity line |

Post-deploy actions — **MUST:** status tiles, URLs, (next) tail logs + restart.
**SHOULD:** edit a config value → Apply & redeploy. **NICE:** rotation next-date + "rotate
now", "update available" chip.

## Screens (single page, view-switched — no router)
1. **Compose (home)** — conversational entry → a **Plan card**: services + capability
   chips + per-service **⚙ Tune** expander (replica stepper + config form + secret keys) +
   the rotation summary, then **Install**.
2. **Install stream** — optimistic step list, SSE status dots, final "see Stacks".
3. **Stacks** — living tiles from `/api/status` (cluster, services, complete/❉ health dot).
4. **⌘K palette** — global nav + per-app install actions.

## JSON + SSE API (implemented)
`GET /api/services` (catalog + config knobs + secret keys + ha/replicas) ·
`GET /api/branding` · `GET /api/status` (installed stacks from `~/.skbloom/*.json`) ·
`POST /api/propose` (validated profile + named-step plan + rotation summary) ·
`POST /api/up` (SSE install stream; accepts `config_overrides`, `tls`, `seed`).

## Build status
**Done:** token system, ⌘K palette, stacks tiles + skeletons, optimistic SSE install,
form-from-descriptor tune panel (config + replica + secret keys), rotation summary,
config-override → deploy.
**Next (SHOULD/NICE):** tail-logs + restart endpoints (need live-cluster plumbing),
Apply-&-redeploy, scale, "Why this?" plan explainability, update-available chips.

Sources: Dokploy/Coolify/Komodo/Glasskube comparisons (2026), UX-trend research
(calm interfaces, transparent AI, command-palette, skeleton screens).
