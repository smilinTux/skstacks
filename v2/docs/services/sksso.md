# sksso — Identity / SSO — human SSO (OIDC/SAML/LDAP) + agent service-auth upgrade path

✅ **deploy-ready** · layer: core · version CHANGEME_VERSION · scope `sksso`

## Capability / Provider
- **Capability:** Identity / SSO — human SSO (OIDC/SAML/LDAP) + agent service-auth upgrade path
- **Provider:** Authentik (human SSO, OIDC/SAML/LDAP); Zitadel as M2M/agent service-auth upgrade path
- **Alternates:** Zitadel (strong M2M/agent service-auth)
- **Platforms:** docker-swarm, kubernetes
- **HA:** yes — SSO is load-bearing, `min_replicas: 3` behind the Gateway

## Topology

```mermaid
flowchart TB
  subgraph backend["Secret backend (skvault)"]
    S1[authentik_secret_key]
    S2[authentik_db_password]
    S3[authentik_redis_password]
  end
  subgraph swarm["Swarm: ${ENV}"]
    A["authentik-server<br/>(ghcr.io/goauthentik/server:2024.10)<br/>cmd server · ports 9000, 9443<br/>vol media, templates"]
  end
  subgraph k8s["K8s: ESO"]
    ES[ExternalSecret v1] --> SEC[Secret] -->|envFrom UPPERCASE| A2["Deployment authentik-server<br/>replicas 3, 1/node"]
  end
  S1 & S2 & S3 --> A
  S1 & S2 & S3 --> ES
  skdata[(skdata)] -.depends_on.-> A
  skcache[(skcache)] -.depends_on.-> A
  skfence -.required_by.-> A
```

## Secrets

| key | rotation_days | required | description |
|-----|---------------|----------|-------------|
| `authentik_secret_key` | 365 | yes | Authentik `SECRET_KEY` (Django signing) — sensitive |
| `authentik_db_password` | — | yes | PostgreSQL password for Authentik database — sensitive |
| `authentik_redis_password` | — | no | Redis/Valkey password for Authentik cache — sensitive |

## Config
`LOG_LEVEL=INFO` · `DOMAIN=${SKSTACKS_DOMAIN}` · `CLUSTER=${SKSTACKS_CLUSTER}`

## Deploy
- **Container/image:** `authentik-server` / `ghcr.io/goauthentik/server:2024.10`
- **Command:** `server`
- **Ports:** `9000:9000`, `9443:9443`
- **Volumes:** `authentik-media:/media`, `authentik-templates:/templates`
- **HA/replicas:** `min_replicas: 3` (behind the Gateway)
- **Healthcheck:** `["CMD","ak","healthcheck"]` (30s/5s/3)

Renders to **both** Swarm compose and K8s manifests via `skrender`. K8s materializes an ESO **ExternalSecret** (`external-secrets.io/v1`) synced into a **Secret** consumed via `envFrom` (UPPERCASE env names, consistent with Swarm's `${ENV}`).

## Dependencies
- **depends_on:** `skdata`, `skcache`
- **required_by:** listed as `required_by` of `skfence`
