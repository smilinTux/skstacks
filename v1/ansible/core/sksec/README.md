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

## Traefik auto-detection

`sksec` reads Traefik's access logs to build its detections, so it needs to
know which Traefik service is running. `shared/tasks/detect_traefik_setup.yml`
auto-detects `skfence` (single-node) vs `skfenceha` (multi-node HA) from the
running Docker services (falling back to on-disk config directories), and
sets `traefik_service_name` accordingly. Set `sksec.TRAEFIK_HA_MODE: true`
in vault to force HA-mode log paths and pin CrowdSec to the ACME master node
for exclusive database write access; leave it `false` (default) for a
single-node `skfence` cluster.

## Bouncer key

`CROWDSEC_BOUNCER_API_KEY` is optional in vault. If unset, the playbook
generates one with `openssl rand -base64 32` on first deploy and persists it
at `/var/data/config/sksec-<env>/secret/bouncer.key` (mode 0640, owned by
root) so re-running the playbook does not rotate it under you. Set it in
vault only to pin a specific key.

## Required instance vars (vault)

`sksec-<env>_vault.yml` (see the framework README's "Instance contract" for
the lookup path):

```yaml
sksec:
  # REQUIRED - no framework default, these are estate-specific
  CLUSTERNAME: "<your-cluster-name>"
  DOMAIN: "<your-domain>"
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
  TRAEFIK_HA_MODE: false         # true if the cluster runs skfenceha

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
