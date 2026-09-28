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

`sksec.ALLOWLIST_CIDRS` (list of IPs and CIDRs, default empty) renders a
CrowdSec whitelist parser into
`/var/data/runtime/sksec-<env>/crowdsec-config/parsers/s02-enrich/sksec-allowlist.yaml`.
Events from those sources are dropped before any scenario sees them, so
they never raise an alert or a local ban. List the operator's own ranges:
LAN, VPN (e.g. tailscale `100.64.0.0/10`), the cluster nodes, the overlay
and ingress networks, and the site's own public IP (clients that reach the
edge through hairpin NAT arrive from it). Manual `cscli decisions add` and
blocklist decisions are not filtered. Emptying the list removes the file.

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
  CROWDSEC_AGENT_HOST: "sksec-prod_crowdsec:8080"
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
  CROWDSEC_IMAGE: ""             # default crowdsecurity/crowdsec:v1.6.11 (pin a digest here)
  BOUNCER_IMAGE: ""              # default docker.io/fbonalair/traefik-crowdsec-bouncer:0.5.0

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
