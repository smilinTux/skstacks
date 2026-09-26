# capauth — Sovereign M2M auth — PGP-based identity and secret access without OAuth

📋 **descriptor-only** · layer: core · version CHANGEME_VERSION · scope `capauth`

**Status:** 📋 descriptor-only — `deploy:` block removed, TODO. capauth is not a
validated v2 stack today. The service that exists (`capauth-service`, a FastAPI
app in the [CapAuth](https://github.com/smilinTux/capauth) repo) runs in
production as a **loopback systemd unit**, not a container this repo deploys —
see Real deployment below and the open design question before filling this
back in.

An earlier version of this descriptor carried a `deploy:` block claiming an HA,
3-replica stack on port `8081:8081` from `ghcr.io/smilintux/capauth:latest`.
That image is real (pushed once, 2026-06-16, never rebuilt since) but nothing
in the fleet has ever run it: it does not match the port the service actually
listens on (`8420`), and it is not what the now-decommissioned `capauth-prod`
stack ran (a separately, locally built `capauth-service:latest` image — same
source, different build, never published). The block has been removed rather
than repaired, because the shape it described (exposed HA replicas) contradicts
how this service is designed to run — see below.

## Capability / Provider
- **Capability:** Sovereign M2M auth — PGP-based identity and secret access without OAuth
- **Provider:** CapAuth (sovereign PGP-based; skcapstone MCP integration)
- **Alternates:** Zitadel (OIDC M2M for human+agent hybrid); SPIFFE/SPIRE (workload identity)
- **Platforms:** docker-swarm, bare-metal
- **HA:** target `min_replicas: 3` ("if you need one, get two" — identity root) —
  aspirational, not deployed as HA today; see open question below

## Real deployment today

CapAuth's own SOP documents three deployment shapes for the same
`capauth-service` FastAPI app. None of them is what this descriptor described.

**What actually runs on a fleet node — a loopback PDP, no ingress:**

```
unit         capauth-authz.service        (systemd --user)
ExecStart    capauth-service --host 127.0.0.1 --port 8420
gate         CAPAUTH_AUTHZ_TOKEN (unset = the decide endpoint answers 503)
consumer     skgateway's authz PEP, co-located on the same node,
             calling POST /v1/authz/decide (SKGATEWAY_AUTHZ_ENFORCE=1)
self-report  GET /capauth/v1/status (there is no /healthz route)
```

`127.0.0.1` / `8420` are the code's own defaults (`--host`/`--port` in
`capauth/src/capauth/service/server.py`), not just this unit's flags — passing
`0.0.0.0` still works but prints a startup warning, because a PDP is not meant
to sit on a public interface. This is a **bare console-script process under a
per-user systemd unit**, not a container and not a cluster workload — nothing
this repo would render as Swarm compose or a K8s Deployment today.

**Other shapes that exist but are not deployed from this repo:**
- A standalone, containerized `capauth-service` (`deploy/capauth-service/docker-compose.yml`
  in the CapAuth repo) publishing `8420:8420` from `ghcr.io/smilintux/capauth:latest` —
  this is the image the old `deploy:` block referenced, just on the wrong port and
  without the compose file's warning that a bare `ports:` mapping binds all
  interfaces. This was the shape `capauth-prod` ran (from a locally built,
  unpublished image) before it was decommissioned 2026-09-26 (zero real traffic
  in 7 days; no Forgejo/Nextcloud/Immich integration ever wired up).
- A custom Authentik image (`ghcr.io/smilintux/authentik-capauth`, actively
  published, 14 versions) that bundles a CapAuth PGP passwordless login stage
  into Authentik itself — a different artifact than the `capauth` package, and
  a different service (`sksso`'s job, not `capauth`'s).

## Open design question

The live PDP is intentionally a loopback singleton: the server code itself
warns against binding a public interface, because it decides authz for one
co-located gateway, not a cluster. A `min_replicas: 3` / exposed-port stack
descriptor (the shape this file used to claim) does not fit that model as-is.
Before a `deploy:` block goes back in, someone needs to decide: does `capauth`
in the v2 catalog mean *this* PDP (in which case "HA" means something other
than "expose it and scale replicas" — maybe per-node loopback instances behind
no shared ingress at all), or does it mean the standalone verification-service
container shape (Scenario B above), which *can* be exposed and scaled but is
currently unused in prod? Those are two different services wearing one name.
Chef owns this call.

## Secrets

| key | rotation_days | required | description |
|-----|---------------|----------|-------------|
| `capauth_admin_token` | 90 | yes | Bearer token gating the capauth-service admin API (`CAPAUTH_ADMIN_TOKEN`) — sensitive |
| `capauth_jwt_secret` | 90 | yes | Secret signing CapAuth-issued session JWTs (`CAPAUTH_JWT_SECRET`) — sensitive |
| `capauth_authz_token` | 90 | yes | Bearer token gating the `/v1/authz/decide` PDP endpoint (`CAPAUTH_AUTHZ_TOKEN`); unset = disabled — sensitive |
| `capauth_server_key_armor` | — | no | Optional armored PGP key for signing challenge nonces (`CAPAUTH_SERVER_KEY_ARMOR`) — sensitive |
| `capauth_server_key_passphrase` | — | no | Passphrase for `capauth_server_key_armor` (`CAPAUTH_SERVER_KEY_PASSPHRASE`) — sensitive |

These are the `capauth-service` process's own runtime secrets. They are
distinct from the per-agent CapAuth PGP identity keys under `~/.capauth/`,
which are provisioned per agent and never flow through this descriptor.

## Config
`LOG_LEVEL=INFO` · `DOMAIN=${SKSTACKS_DOMAIN}` · `CLUSTER=${SKSTACKS_CLUSTER}`

## Dependencies
- **depends_on:** none
- **required_by:** none declared (identity root; also a `skvault` adapter option)
