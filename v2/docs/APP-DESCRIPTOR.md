# v2 App Descriptor Schema (`app.yaml`)

Every capability port in v2 is described by an `app.yaml` file at
`v2/<C>/<port>/app.yaml`. This document specifies the schema, field semantics,
and the intentional relationship to the skos foundation's `app.yaml`.

---

## Schema

```yaml
# Required fields
name:        <string>   # port identifier -- matches the directory name (e.g. skfence)
capability:  <string>   # one-line description of what the PORT does (technology-agnostic)
description: <string>   # one-line description of this specific adapter configuration
scope:       <string>   # secret backend path prefix (usually == name)
version:     <string>   # adapter version or CHANGEME_VERSION for stubs
platforms:   [<list>]   # target platforms: docker-swarm, kubernetes, rke2, k3d, bare-metal

# Adapter fields
provider:    <string>   # recommended sovereign adapter -- name + 1-line reason
alternates:  [<list>]   # interop adapters from the capability map

# Secrets block (values NEVER live here -- only key names)
secrets:
  - key:          <string>   # secret key name (resolved from backend at deploy time)
    description:  <string>   # human description of what this secret is
    rotation_days: <int>      # recommended rotation interval (days)
    required:     <bool>     # true = deploy will fail without this key
    sensitive:    <bool>     # true = mask in logs and CI output (default: false)

# Non-sensitive runtime config (overridden by overlay values; no raw secrets here)
config:
  KEY: VALUE

# Health check endpoint (fill in before deploy; CHANGEME_HOST in stubs)
healthcheck:
  url:      <string>
  interval: <duration>
  timeout:  <duration>
  retries:  <int>

# Optional dependency declarations
depends_on:  [<list>]   # ports that must be deployed before this one
required_by: [<list>]   # ports that depend on this one (informational)
```

---

## Field reference

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Lowercase. Must match directory name exactly. |
| `capability` | yes | Describes the *port* (stable interface) -- technology-agnostic. |
| `description` | yes | Describes the *adapter* (current provider choice). |
| `scope` | yes | Secret backend path prefix -- usually identical to `name`. |
| `version` | yes | Adapter version string. Use `CHANGEME_VERSION` in stubs. |
| `platforms` | yes | One or more of: `docker-swarm`, `kubernetes`, `rke2`, `k3d`, `bare-metal`. |
| `provider` | yes | Recommended sovereign adapter name + reason. From the capability map. |
| `alternates` | yes | Interop/alt adapters. Use `alternates: []` if none apply. |
| `secrets[].key` | yes (per secret) | Key name only. No values, ever. |
| `secrets[].description` | yes | Human-readable explanation. |
| `secrets[].sensitive` | no | Default false. Set true to mask in logs. |
| `config` | no | Non-secret runtime config. Overridden by `overlays/<env>/values.yaml`. |
| `healthcheck` | no | Fill before deploy. Stubs use `CHANGEME_HOST`. |
| `depends_on` | no | Ports that must exist before this one is deployed. |
| `required_by` | no | Ports that reference this one (informational only). |

---

## Relationship to the skos Foundation `app.yaml`

There are intentionally **two** `app.yaml` layers in this architecture:

| Layer | Location | Purpose |
|---|---|---|
| **v2 descriptor** (this spec) | `v2/<C>/<port>/app.yaml` | Deployment-engine spec: names the adapter, declares secret key refs, config defaults, platform targets. v2 uses this to render manifests and inject secrets at deploy time. |
| **skos foundation descriptor** | `$SK_DATA_ROOT/apps/<app>/app.yaml` | OS-level capability/packaging spec: binds a capability to a `PackagingAdapter` (oci/native) for a topology profile (local/cluster/cloud). skos uses this to install, locate, and health-check apps via `skos install`. |

**The two layers are complementary, not conflicting.** The v2 descriptor answers
"what are this port's secrets and config?"; the skos descriptor answers "how do I
build and deploy this app for a given profile?". When skos invokes v2 to deploy a
capability, the skos descriptor resolves the packaging adapter, then v2's descriptor
provides the secret and config contract.

For the first skos proof apps (`capauth`, `skmemory`), both descriptors will
exist. The v2 one lives here; the skos one lives in the `smilinTux/skos` repo.

---

## Adding a new port

1. Create `v2/<C>/<port>/` in the correct 4C category (see `CONVENTIONS.md`).
2. Copy `app.yaml` from a neighboring port as a template.
3. Fill in all required fields. Set `version: CHANGEME_VERSION` for stubs.
4. Set `provider` and `alternates` from the capability map.
5. Add only secret *key names* in `secrets:`. Never put values or `CHANGEME_VALUE`.
6. Register the new port in `CONVENTIONS.md`.

---

## Example -- complete normalized stub

```yaml
# SKStacks v2 -- <port> app descriptor (STUB)
# Structure only -- NO real secrets. Replace CHANGEME_* before deploy.

name: <port>
description: "<adapter> -- one-line description of this specific adapter choice."
capability: "<port capability in technology-agnostic terms>"
scope: <port>
version: "CHANGEME_VERSION"
platforms: [docker-swarm, kubernetes]
provider: <Adapter Name> (<why it was chosen>)
alternates:
  - <Alt Adapter 1> (<use case where you would choose it>)
  - <Alt Adapter 2>

secrets:
  - key: <secret_key_name>
    description: "<human description>"
    rotation_days: 90
    required: true
    sensitive: true

config:
  LOG_LEVEL: INFO
  DOMAIN: "${SKSTACKS_DOMAIN}"
  CLUSTER: "${SKSTACKS_CLUSTER}"

healthcheck:
  url: "https://CHANGEME_HOST.${SKSTACKS_DOMAIN}/healthz"
  interval: 30s
  timeout: 5s
  retries: 3
```
