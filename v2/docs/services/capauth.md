# capauth: Sovereign M2M auth, PGP-based identity and secret access without OAuth

📋 **descriptor-only** · layer: core · version CHANGEME_VERSION · scope `capauth`

**Status:** 📋 descriptor-only (no `deploy:` block, by design). capauth in
this catalog is the loopback authz PDP that runs in production today
(`capauth-service`, a FastAPI app in the
[CapAuth](https://github.com/smilinTux/capauth) repo), not a container this
repo deploys. See Real deployment below and the Decision section for what
was resolved and why.

An earlier version of this descriptor carried a `deploy:` block claiming an HA,
3-replica stack on port `8081:8081` from `ghcr.io/smilintux/capauth:latest`.
That image is real (pushed once, 2026-06-16, never rebuilt since) but nothing
in the fleet has ever run it: it does not match the port the service actually
listens on (`8420`), and it is not what the now-decommissioned `capauth-prod`
stack ran (a separately, locally built `capauth-service:latest` image, same
source, different build, never published). The block was removed rather
than repaired, because the shape it described (exposed HA replicas)
contradicts how this service is designed to run, and because it named a
deployment mode (the standalone verification-service container) that this
catalog does not mean by `capauth` (see Decision below).

## Capability / Provider
- **Capability:** Sovereign M2M auth: PGP-based identity and secret access without OAuth
- **Provider:** CapAuth (sovereign PGP-based; skcapstone MCP integration)
- **Alternates:** Zitadel (OIDC M2M for human+agent hybrid); SPIFFE/SPIRE (workload identity)
- **Platforms:** docker-swarm, bare-metal
- **HA:** `min_replicas: 3` means at least 3 independent per-node PDP
  instances fleet-wide, each loopback-bound and serving its own co-located
  gateway. It does not mean a clustered/replicated service behind shared
  ingress; see Decision below.

## Real deployment today

CapAuth's own SOP documents deployment shapes for the same `capauth-service`
FastAPI app. Only the first one is what `capauth` means in this catalog.

**What actually runs on a fleet node (a loopback PDP, no ingress):**

```
unit         capauth-authz.service        (systemd --user)
ExecStart    capauth-service --host 127.0.0.1 --port 8420
gate         CAPAUTH_AUTHZ_TOKEN (unset = the decide endpoint answers 503)
consumer     skgateway's authz PEP, co-located on the same node,
             calling POST /v1/authz/decide (SKGATEWAY_AUTHZ_ENFORCE=1)
self-report  GET /capauth/v1/status (there is no /healthz route)
```

`127.0.0.1` / `8420` are the code's own defaults (`--host`/`--port` in
`capauth/src/capauth/service/server.py`), not just this unit's flags. Passing
`0.0.0.0` still works but prints a startup warning, because a PDP is not meant
to sit on a public interface. This is a **bare console-script process under a
per-user systemd unit**, not a container and not a cluster workload, and
nothing this repo renders as Swarm compose or a K8s Deployment for it today.

**Other shapes that exist in the CapAuth repo but are not what this
descriptor covers:**
- **DEPRECATED (2026-09-26):** a standalone, containerized `capauth-service`
  (`deploy/capauth-service/docker-compose.yml` in the CapAuth repo)
  publishing `8420:8420` from `ghcr.io/smilintux/capauth:latest`, i.e. the
  public verification-service mode (challenge/verify/callback endpoints for
  passwordless PGP OIDC login). This is the image the old `deploy:` block
  referenced, on the wrong port. It is the shape `capauth-prod` ran (from a
  locally built, unpublished image) before that stack was decommissioned
  2026-09-26 (zero real traffic in the prior 7 days; no
  Forgejo/Nextcloud/Immich integration was ever wired up). Reason for
  deprecation: PGP passwordless login for apps is now covered by
  `ghcr.io/smilintux/authentik-capauth` below, and the loopback PDP is the
  only shape this catalog runs. See the CapAuth repo's own README/SOP for the
  deprecation note.
- A custom Authentik image (`ghcr.io/smilintux/authentik-capauth`, actively
  published, 14 versions) that bundles a CapAuth PGP passwordless login stage
  into Authentik itself, a different artifact than the `capauth` package and
  a different service (`sksso`'s job, not `capauth`'s). This is the
  replacement for the deprecated standalone verification service above.

## Decision

**Decided by Chef (operator), 2026-09-26.** capauth in this catalog is the
per-node loopback PDP described above, run as a systemd service on each node
that runs a PEP (skgateway), never a Swarm/K8s stack. There is no `deploy:`
block by design, and none should be added for this shape; if one is ever
written it must model a per-node/host-scoped unit (an Ansible role or
host-plugin pattern), not a clustered service with `replicas:`. HA for this
descriptor means "every node that needs a PDP runs its own instance", not
"expose one service and scale replicas behind shared ingress". The standalone
verification-service mode and the `ghcr.io/smilintux/capauth` image are
deprecated for the reasons above. Full writeup:
[`docs/decisions/capauth-loopback-pdp.md`](../../../docs/decisions/capauth-loopback-pdp.md).

## Secrets

| key | rotation_days | required | description |
|-----|---------------|----------|-------------|
| `capauth_admin_token` | 90 | yes | Bearer token gating the capauth-service admin API (`CAPAUTH_ADMIN_TOKEN`), sensitive |
| `capauth_jwt_secret` | 90 | yes | Secret signing CapAuth-issued session JWTs (`CAPAUTH_JWT_SECRET`), sensitive |
| `capauth_authz_token` | 90 | yes | Bearer token gating the `/v1/authz/decide` PDP endpoint (`CAPAUTH_AUTHZ_TOKEN`); unset = disabled, sensitive |
| `capauth_server_key_armor` | n/a | no | Optional armored PGP key for signing challenge nonces (`CAPAUTH_SERVER_KEY_ARMOR`), sensitive |
| `capauth_server_key_passphrase` | n/a | no | Passphrase for `capauth_server_key_armor` (`CAPAUTH_SERVER_KEY_PASSPHRASE`), sensitive |

These are the `capauth-service` process's own runtime secrets. They are
distinct from the per-agent CapAuth PGP identity keys under `~/.capauth/`,
which are provisioned per agent and never flow through this descriptor.

## Config
`LOG_LEVEL=INFO` · `DOMAIN=${SKSTACKS_DOMAIN}` · `CLUSTER=${SKSTACKS_CLUSTER}`

## Dependencies
- **depends_on:** none
- **required_by:** none declared (identity root; also a `skvault` adapter option)
