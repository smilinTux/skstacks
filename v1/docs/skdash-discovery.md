# skdash: generic Traefik-API discovery

skdash deploys [Dashy](https://github.com/lissy93/dashy) and can populate its
sections either automatically, from the cluster's own Traefik API, or from a
static list an instance writes into its vault. This document is the design
for the publishable, estate-free version of that feature.

## How discovery works today (private, estate-specific)

The private prod playbook (`optional/skdash/tasks/generate_config.yml`)
shells out to `src/skdash/generate_dashy_config.py`, a ~580-line script that:

- **Builds the Traefik API URL by convention**: `https://skfence[-{env}].{{
  cluster }}.{{ domain }}`, assuming a component literally named `skfence`
  exists at a specific hostname pattern on that domain.
- **Calls the API unauthenticated** (`GET /api/http/routers`, no auth
  header) and, on failure, falls back to reading Docker Swarm service
  labels directly on the manager node.
- **Extracts the FQDN** from each router's `Host()` rule via regex.
- **Filters nothing**: every router with a `Host()` rule becomes an item;
  there is no way to exclude the dashboard's own router or an internal-only
  one.
- **Detects "technology" and icon** via two large hardcoded dictionaries
  keyed on this estate's own service names (`skfence`, `skgit`, `skmon`,
  `skgentis`, `skform`, ...) and matches them against this estate's real
  domain and cluster name (both hardcoded into the script's own defaults).
- **Categorizes into 9 fixed sections** ("Core Infrastructure", "Security
  Services", ...) using the same hardcoded name substrings.
- **Merges with vault**: the generated config is a base dict; any
  `skdash.*` vault key overrides it, and a vault `skdash.sections` array
  replaces the generated sections outright (`tasks/generate_config.yml`'s
  `combine(..., recursive=True)`).

None of the URL convention, the auth assumption, the technology map or the
category list is safe to publish: they hardcode this estate's domain,
cluster name, node-naming convention and internal service catalog. A
previous attempt to publish skdash simply deleted all of this and shipped a
static vault-only `skdash.sections` list. That is exactly the config the
private prod deploy currently overrides via discovery, so shipping that
would silently regress the live dashboard on the next `git pull` of the
public framework.

## What is estate-specific vs. generic

| Piece | Estate-specific | Generic |
|---|---|---|
| Traefik API reachable at a URL | the URL itself (a `skfence.<cluster>.<domain>` hostname built from this estate's own naming convention) is this estate's convention | "query some URL for `/api/http/routers`" is a Traefik API contract every instance shares |
| Auth to reach that URL | none needed on this estate (network-segmented) | other instances may put Traefik's API behind basic/bearer auth; the framework should support a secret without assuming one is always required |
| FQDN extraction (`Host()` regex) | no | pure Traefik rule syntax |
| Technology/icon map (`skfence`->`traefik`, `skgentis`->`treasury`, ...) | yes, keyed on this estate's private service names | the *mechanism* (map a pattern to a group/icon) is generic; the *entries* are not |
| 9 fixed categories | yes, this estate's taxonomy | the *mechanism* (bucket by pattern into a named section) is generic; the categories are not |
| Docker-label fallback | somewhat (assumes manager-node Docker access, which every swarm node has, but it duplicates the API path for no real gain) | dropped: the static-sections fallback below covers "API unreachable" more simply and without a second discovery code path to maintain |
| Vault-overrides-everything merge | no | this is exactly the mechanism a generic framework needs |

## Proposed generic design

**Discovery is ON by default**, driven entirely by instance-supplied vars
under `skdash.discovery.*` (no framework defaults reference any real
domain, cluster or service name):

```yaml
skdash:
  CLUSTERNAME: mycluster          # required, no default
  DOMAIN: example.com             # required, no default
  discovery:
    enabled: true                 # default: true
    traefik_api_url: "https://traefik.example.com"   # required when enabled, no default
    traefik_api_token: "{{ vault_skdash_traefik_token }}"  # required when enabled, no default; set to "" if the API needs no auth
    timeout: 10                   # optional, default: 10 seconds
    validate_certs: true          # optional, default: true
    exclude_patterns:             # optional, default: []
      - "^traefik"                # e.g. drop Traefik's own dashboard router
      - "^skdash"                 # e.g. drop skdash's own router
    group_map:                    # optional, default: [] (everything falls into one default group)
      - pattern: "^grafana|^prometheus"
        group: "Monitoring"
        icon: "grafana"
    default_group_name: "Discovered Services"   # optional
    extra_sections: []            # optional: static sections pinned alongside discovery results
  sections: []                    # static fallback, used when discovery is off or unreachable
```

- `traefik_api_url` and `traefik_api_token` carry **no literal default**.
  Ansible fails closed: an instance that turns discovery on and forgets to
  set them gets an "undefined variable" error at deploy time, not a
  silently-shared public value. An instance whose API needs no auth sets
  the token to `""` explicitly: that is a deliberate per-instance choice,
  not a framework default.
- The Traefik API is called once, with a short timeout, wrapped in
  `ignore_errors: true`. Whatever happens (DNS failure, timeout, non-200,
  malformed JSON), the deploy proceeds.
- **Fallback rule**: sections come from discovery **only if** discovery is
  enabled **and** the API call actually returned usable JSON this run.
  Otherwise sections come from the instance's static `skdash.sections`.
  This is a single boolean decision (`skdash_discovery_available`), not a
  partial/best-effort merge, so the two code paths are easy to reason
  about and to test.
- **Transform mechanism, not estate data**: the router-to-section logic
  (`filter_plugins/skdash_discovery.py`, a plain Python module with no
  Ansible dependency) knows nothing about SKStacks service names. It:
  1. extracts the host from each router's `Host()` rule (routers with no
     `Host()`, such as catch-alls or path-only rules, are skipped);
  2. drops any router whose name or host matches an `exclude_patterns`
     regex;
  3. buckets the rest into a section by the first matching `group_map`
     entry, or `default_group_name` if none match;
  4. builds a title from the router name (stripping Traefik's `@provider`
     suffix) and a URL from the host + whether a `*secure*` entrypoint is
     present.
  All estate categorization (which services exist, what to call them, what
  icon they get) lives in the instance's own vault `group_map`/`exclude_patterns`,
  not in the framework.
- **Icons**: discovered items get one default icon
  (`dashboard-icons/svg/default.svg`, a public, generic icon set already
  used by the framework). Per-service icons are the instance's job via
  `group_map[].icon` (section-level) or by hand-pinning items through
  `discovery.extra_sections`, which are always appended after the
  discovered sections. No per-service icon table ships in the framework.
- **Vault override semantics unchanged for everything except sections**:
  `page_title`, `theme`, etc. still use plain `{{ skdash.X | default(...) }}`
  in `config.yml.j2`, so vault always wins on those. `sections` specifically
  follows the on/off + fallback rule above rather than a partial merge,
  because "merge discovered and static sections" produced confusing,
  hard-to-predict dashboards in the old design (a stray vault `sections: []`
  silently nuked discovery *and* looked identical to "discovery is off").

### Files

- `v1/ansible/optional/skdash/filter_plugins/skdash_discovery.py`: the pure
  transform (`routers_to_sections`) and the fallback decision
  (`select_sections`); no Ansible import, unit tested directly.
- `v1/ansible/optional/skdash/tasks/generate_config.yml`: the Ansible glue
  that reads `skdash.discovery.*`, calls the API with `uri`, calls the
  filter plugin, and sets `skdash_effective_sections`.
- `v1/ansible/optional/skdash/src/config/skdash/config.yml.j2`: renders
  whatever `skdash_effective_sections` is; the template does not know or
  care whether that came from discovery or the static fallback.
- `v1/ansible/optional/skdash/deploy_skdash-{dev,staging,prod}.yml`: the
  same task list in all three (only `env` and network subnets differ), per
  framework convention.

## What prod must set to keep today's dashboard

The current prod dashboard is produced entirely by discovery (the
private `generate_dashy_config.py` script), with no `skdash.sections`
override in the prod vault. To reproduce the same behavior on the
generic framework, prod's vault needs:

```yaml
skdash:
  CLUSTERNAME: <existing cluster name>
  DOMAIN: <existing domain>
  discovery:
    enabled: true
    traefik_api_url: "https://<existing skfence hostname>"
    traefik_api_token: ""     # the existing API call sends no auth today
    exclude_patterns:
      - "^skdash"              # skdash's own router shouldn't list itself
    group_map:
      # one entry per category the old script hardcoded, e.g.:
      - { pattern: "skfence|skfenceha|traefik", group: "Core Infrastructure" }
      - { pattern: "sksec|crowdsec", group: "Security Services" }
      - { pattern: "skgit|forgejo|gitea|skport|portainer", group: "Development Tools" }
      - { pattern: "skmon|prometheus|grafana|elasticsearch|kibana|loki", group: "Monitoring Stack" }
      - { pattern: "skstor|minio|sksync|syncthing", group: "Storage Services" }
      - { pattern: "skorch|n8n", group: "Workflow Automation" }
      - { pattern: "sketh|geth|lighthouse|skxrpld|xrpl", group: "Blockchain Services" }
      - { pattern: "skmatrix|matrix|skgentis|skform", group: "Business Applications" }
    default_group_name: "Other Services"
```

The generated titles will read "Skfence" instead of a curated display name
and icons will be the generic default icon instead of per-technology icons
from `dashboard-icons`; both are cosmetic and can be layered back in with
`discovery.extra_sections` or per-item vault overrides if wanted. The
important behavior (grouping, host discovery, live updates when a new
router appears) is preserved.
