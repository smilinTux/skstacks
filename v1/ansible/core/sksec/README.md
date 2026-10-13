# sksec (CrowdSec)

CrowdSec plus a Traefik bouncer, providing behavioral intrusion detection and
automated blocking for every service behind Traefik in the cluster. A core
service (deployed once per cluster, like skfence/skfenceha).

## How it protects Traefik

`sksec` renders a Traefik dynamic-config file (`dynamic/sksec.yml`) defining
a `crowdsec-bouncer` forwardAuth middleware that talks to the bouncer
service over the shared `sksec-<env>` network. Add `crowdsec-bouncer@file`
to a router's middleware chain (or the framework's `default@file` chain) to
put it under CrowdSec's protection.

The bouncer fails closed: when it cannot reach the CrowdSec LAPI, or its key
is not registered, it answers 403 for every request it is asked about, and
Traefik returns an error when the bouncer itself is down. Put one low-value
router behind `crowdsec-bouncer@file` first and widen only after the checks
below pass.

`dynamic/sksec.yml` is written from the deploy node onto the shared
filesystem. A Traefik file provider on another node watching that directory
over NFS gets no inotify event for a remote write, so it may not load the
middleware until that node's Traefik restarts (or the file is rewritten
locally on that node). Check each node before a router references it.

## Log acquisition

CrowdSec tails the Traefik access logs read-only from the shared log dir:
`skfenceha` writes `/var/data/logs/skfenceha-<env>/traefik/{worker,acme}/access-<node>.log`,
`skfence` writes `/var/data/logs/skfence-<env>/traefik/<node>/access.log`.
`acquis.yaml` polls them (`poll_without_inotify: true`, since other nodes'
writes raise no inotify event on the CrowdSec node) and labels them
`type: traefik` for the stock `crowdsecurity/traefik-logs` parser, so
`COLLECTIONS` must include `crowdsecurity/traefik`. Check with
`cscli metrics` (Acquisition Metrics: lines read and parsed per file).

## Traefik auto-detection

`sksec` reads Traefik's access logs to build its detections, so it needs to
know which Traefik service is running. `shared/tasks/detect_traefik_setup.yml`
auto-detects `skfence` (single-node) vs `skfenceha` (multi-node HA) from the
running Docker services (falling back to on-disk config directories), and
sets `traefik_service_name` accordingly. Set `sksec.TRAEFIK_HA_MODE: true`
in vault to force HA-mode log paths and pin CrowdSec to the node labelled
`traefik.acme.master=true`; leave it `false` (default) to let Swarm place it.
skfenceha's own deploy owns that label: sksec only reads it and never adds
or removes it.

## Bouncer key

`CROWDSEC_BOUNCER_API_KEY` is optional in vault. If unset, the playbook
generates one with `openssl rand -base64 32` on first deploy and persists it
at `/var/data/config/sksec-<env>/secret/bouncer.key` (mode 0640, owned by
root) so re-running the playbook does not rotate it under you. Set it in
vault only to pin a specific key. The key (vault or persisted) is rendered
into `sksec.env` (mode 0640) as both `CROWDSEC_BOUNCER_API_KEY` (read by the
bouncer) and `BOUNCER_KEY_traefik`, from which the CrowdSec image registers
bouncer `traefik` when the container starts. It never re-keys an existing
bouncer: to rotate, change the key, redeploy, then
`cscli bouncers delete traefik` and restart the crowdsec service.

## Allowlist

`sksec.ALLOWLIST_CIDRS` (list of IPs and CIDRs, default empty) is the
operator's own ranges that must never be banned: LAN, any VPN address range,
the cluster nodes, the overlay and ingress networks, CI runners, and the
site's own public IP (clients that reach the edge through hairpin NAT arrive
from it). The deploy applies it in two places, because they cover different
decisions:

| Where | What it filters |
|---|---|
| Whitelist parser `/var/data/runtime/sksec-<env>/crowdsec-config/parsers/s02-enrich/sksec-allowlist.yaml` | LOCAL detections only: events from those sources are dropped before any scenario sees them, so they never raise an alert or a local ban. |
| LAPI allowlist `sksec-operator` (CrowdSec 1.6.8+, default image v1.6.11) | EVERY decision LAPI hands the bouncer, including the community blocklist (CAPI), console blocklists and a manual `cscli decisions add` (refused with "is allowlisted ... use --bypass-allowlist"). |

The parser alone does not stop a CAPI or blocklist decision from banning an
allowlisted address at the bouncer; the LAPI allowlist does.

After the stack deploy the playbook resolves the node running the crowdsec
task (`docker service ps`; with `TRAEFIK_HA_MODE` that is the ACME master,
often not the deploy node), waits until `cscli lapi status` answers (up to
`LAPI_WAIT_SECONDS`, default 300), then makes the LAPI allowlist hold exactly
`ALLOWLIST_CIDRS`: it creates the list when missing (description
"sksec.ALLOWLIST_CIDRS (operator ranges, applies to CAPI/blocklist/cscli
decisions)"), adds missing entries (comment `sksec.ALLOWLIST_CIDRS`) and
removes entries that are no longer listed. Values are compared as written
(`198.51.100.7` and `198.51.100.7/32` are different entries). An empty list
empties that allowlist (the parser file is removed too); other allowlists,
such as console-managed ones, are never touched. The task reports changed
only when it changed something, and fails the play if LAPI never answers or
the allowlist does not match afterwards.

| Key | Default | Meaning |
|---|---|---|
| `ALLOWLIST_CIDRS` | `[]` | YAML list of IPs and CIDRs (a plain string fails the play). |
| `LAPI_ALLOWLIST_ENABLED` | `true` | `false` skips the LAPI sync (the parser is still rendered). |
| `LAPI_ALLOWLIST_NAME` | `sksec-operator` | Name of the LAPI allowlist the deploy owns. Renaming leaves the old list in place: delete it with `cscli allowlists delete <old>`. |
| `LAPI_WAIT_SECONDS` | `300` | How long to wait for the crowdsec task and its LAPI before failing. |

Checks and fixes, inside the crowdsec container
(`docker exec $(docker ps -q -f name=sksec-<env>_crowdsec) ...` on the node
that runs it):

```bash
cscli allowlists inspect sksec-operator   # entries the deploy manages
cscli allowlists check 192.0.2.10         # is this address allowlisted, and by what
cscli decisions list --ip 192.0.2.10      # any decision still on it
cscli decisions delete --ip 192.0.2.10    # unban an address banned before it was listed
```

Allowlisting does not remove an existing decision: unban with
`cscli decisions delete --ip <ip>` after adding an address that was already
banned.

## Bouncer resilience

Every router behind `crowdsec-bouncer@file` asks the bouncer on every
request, and the bouncer fails closed: with no bouncer answering, those
routers (SSO logins included, if sksso is behind it) fail. By default it is
one replica.

- `BOUNCER_REPLICAS: 2` (or more) runs that many bouncer tasks with
  `max_replicas_per_node: 1`, so losing one node leaves one answering. The
  bouncer is stateless (it asks the LAPI), so replicas need nothing else.
- `BOUNCER_HEALTHCHECK: true` renders an explicit healthcheck running the
  image's own `/healthchecker`, with `BOUNCER_HEALTH_CHECK_INTERVAL`,
  `_TIMEOUT`, `_RETRIES` and `_START_PERIOD`. Note that the image already
  ships a HEALTHCHECK (`/healthchecker`, every 10s, 2 retries) that applies
  when the knob is off; the knob makes it visible in the compose file and
  tunable.

Both default off: an instance that sets neither renders exactly as before.

## Client IP

The bouncer decides on the client address it derives from the request
Traefik forwards. With `BOUNCER_TRUSTED_PROXIES` unset it trusts every hop
(`0.0.0.0/0`) and takes the LEFTMOST `X-Forwarded-For` entry, which the
client controls: a request through a Cloudflare-proxied hostname can carry a
forged first entry and dodge a ban. Set `BOUNCER_TRUSTED_PROXIES` to the
proxies in front of the bouncer: the overlay subnets Traefik reaches it
through (`cloud-public-<env>` and `sksec-<env>`) plus, for proxied
hostnames, the CDN's published ranges (for Cloudflare,
https://www.cloudflare.com/ips/). The bouncer then reads
`X-Forwarded-For` right to left and takes the first address that is not a
trusted proxy. It is rendered as `TRUSTED_PROXIES` into `sksec.env`. The play
refuses `0.0.0.0/0` and `::/0` (that is the unsafe default) and anything
that is not a list of addresses or CIDRs. Check after a deploy with a banned
test address (`cscli decisions add --ip <ip>`) through a proxied hostname.

### Behind a tunnel or CDN

When public traffic reaches Traefik through a tunnel connector (for example
Cloudflare's `cloudflared` pointed at `https://localhost:443`), Traefik sees
the connector's address for every visitor, and by default it drops the
edge's `X-Forwarded-For` because that address is not trusted. CrowdSec then
attributes all visitors to one internal address (which the stock
`crowdsecurity/whitelists` parser discards), and the bouncer checks that one
address: nobody arriving through the tunnel can ever be banned. Three
existing knobs fix it, and an instance that sets none of them renders
exactly as before:

1. skfenceha `TRUSTED_PROXIES`: the connector's source address as Traefik
   sees it, and nothing wider. A connector on the host network of a Traefik
   node reaching a host-mode published port arrives from the
   `docker_gwbridge` gateway (usually `172.18.0.1/32`); check `ClientHost` in
   the access log. Traefik then keeps the edge's `X-Forwarded-For` (the edge
   appends the real client as the last entry) and, because the access log
   sets `ClientHost` to that header, CrowdSec's `crowdsecurity/traefik-logs`
   parser takes its last entry as the source IP. A forged first entry does
   not move it. This is a Traefik static-config change: the workers must be
   restarted to load it.
2. sksec `BOUNCER_TRUSTED_PROXIES`: the overlay subnets Traefik reaches the
   bouncer through (`cloud-public-<env>`, `sksec-<env>`) plus the connector
   address from step 1. The bouncer walks `X-Forwarded-For` right to left
   and takes the first untrusted entry, the real client. Never set step 1
   without this: with it unset the bouncer takes the leftmost entry, which
   the client controls.
3. sksec `ALLOWLIST_CIDRS`: the connector address and the internal ranges
   (RFC 1918, CGNAT/Tailscale, loopback), so that neither a local detection
   nor a community or manual decision can ever ban the tunnel or the
   cluster itself.

What this does not do: the bouncer image reads only `X-Forwarded-For` and
`X-Real-Ip`, not `CF-Connecting-IP`; the real address comes from the
`X-Forwarded-For` the edge sets. Trusting the connector also makes Traefik
pass the other `X-Forwarded-*` headers that arrived through it to the apps
unchanged: an edge such as Cloudflare forwards a client-sent
`X-Forwarded-Host`, and Authentik builds absolute URLs from it. Set
skfenceha `DROP_FORWARDED_HEADERS: [X-Forwarded-Host]` together with step 1
(skfenceha README); apps then use the `Host` header Traefik routed on.

Verify after a deploy: a request through the tunnel logs your own public
address as `ClientHost`; `cscli decisions add --ip <that address>
--duration 2m` turns it into a 403 through the tunnel, and
`cscli decisions delete --ip <that address>` back into the normal answer.

## Required instance vars (vault)

`sksec-<env>_vault.yml` (see the framework README's "Instance contract" for
the lookup path):

```yaml
sksec:
  # OPTIONAL here specifically: falls back to the inventory's own
  # domain/cluster_name host vars (see "Instance contract") if unset, so
  # most instances don't need to set these per-service at all.
  # CLUSTERNAME: "<your-cluster-name>"
  # DOMAIN: "<your-domain>"
  APP_ENV: "prod"          # or dev/staging - must match the env you deploy
  CROWDSEC_AGENT_HOST: "sksec-prod_crowdsec:8080"  # <host>:<port>; unset = this default, a bare name is refused
  GID: "1000"
  COLLECTIONS: "crowdsecurity/linux crowdsecurity/traefik"
  LOG_LEVEL: "info"
  LOG_FORMAT: "json"
  CROWDSEC_CPU_LIMIT: "0.5"
  CROWDSEC_MEMORY_LIMIT: "512M"
  CROWDSEC_CPU_RESERVATION: "0.1"
  CROWDSEC_MEMORY_RESERVATION: "128M"
  BOUNCER_CPU_LIMIT: "0.25"
  BOUNCER_MEMORY_LIMIT: "256M"
  BOUNCER_CPU_RESERVATION: "0.05"
  BOUNCER_MEMORY_RESERVATION: "64M"
  HEALTH_CHECK_INTERVAL: "30s"
  HEALTH_CHECK_TIMEOUT: "5s"
  HEALTH_CHECK_RETRIES: "3"
  HEALTH_CHECK_START_PERIOD: "10s"
  LOG_MAX_SIZE: "10m"
  LOG_MAX_FILES: "3"

  # optional
  CROWDSEC_BOUNCER_API_KEY: ""   # auto-generated and persisted if unset
  CROWDSEC_TIMEOUT: "10"
  INSTANCE: "primary"
  TRAEFIK_HA_MODE: false         # true pins crowdsec to the traefik.acme.master node
  ALLOWLIST_CIDRS: []            # operator-owned IPs/CIDRs never to ban
  LAPI_ALLOWLIST_ENABLED: true   # sync ALLOWLIST_CIDRS into a LAPI allowlist (all decisions, CAPI too)
  LAPI_ALLOWLIST_NAME: "sksec-operator"  # the LAPI allowlist the deploy owns
  LAPI_WAIT_SECONDS: 300         # wait for the crowdsec task and LAPI before failing
  CROWDSEC_IMAGE: ""             # default crowdsecurity/crowdsec:v1.6.11 (pin a digest here)
  BOUNCER_IMAGE: ""              # default docker.io/fbonalair/traefik-crowdsec-bouncer:0.5.0
  BOUNCER_REPLICAS: 1            # >1: that many bouncers, one per node (see "Bouncer resilience")
  BOUNCER_HEALTHCHECK: false     # true: explicit, tunable healthcheck on the bouncer
  BOUNCER_HEALTH_CHECK_INTERVAL: "10s"
  BOUNCER_HEALTH_CHECK_TIMEOUT: "5s"
  BOUNCER_HEALTH_CHECK_RETRIES: 3
  BOUNCER_HEALTH_CHECK_START_PERIOD: "20s"
  BOUNCER_TRUSTED_PROXIES: []    # proxies whose X-Forwarded-For the bouncer believes (see "Client IP")

# optional - override this cluster's sksec overlay network (defaults shown
# are this framework's reserved range; must stay unique per
# v1/tests/test_unique_subnets.py)
sksec_prod_networks:
  - name: "sksec-prod"
    subnet: "172.16.242.0/24"
  - name: "cloud-public-prod"
    subnet: "172.16.200.0/24"
```

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/core/sksec/deploy_sksec-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

## Manual cscli commands

`src/sksec/crowdsec_commands` (and the templated `crowdsec_commands.j2`)
list common `cscli` invocations for installing collections, updating the
hub, and adding/removing IP decisions.
