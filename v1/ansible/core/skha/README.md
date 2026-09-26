# SKHA (High Availability VIP failover)

**SKHA** provides a floating Virtual IP (VIP) for a Docker Swarm manager
cluster using [Keepalived](https://www.keepalived.org/) VRRP (Virtual Router
Redundancy Protocol). The VIP moves to a healthy manager automatically, so
whatever you front with it (Traefik, a Git SSH port, a TURN server, ...)
stays reachable through node failures.

This is a **host** service, not a Swarm stack: it installs and configures
`keepalived` directly on each manager node via `apt`, it does not deploy a
container.

## Per-host deployment (framework exemption)

Every other v1 service selects a single manager and does its work once,
because `/var/data` is shared storage and running the same play on every
manager would race itself. SKHA is the opposite: VRRP identity (priority,
state, unicast peers, network interface) is **per-node**, so this playbook
runs on every host in `target_manager_group`, not a single selected one.

The playbooks carry a `# skstacks: per-host` marker documenting why, and
`v1/tests/test_single_manager_deploy.py` enforces that this exemption stays
narrow: it is only honored under `core/` (host-level core services), and the
marker must state its reason.

## ACME-master VIP locking

If a node carries the label `traefik.acme.master=true` (the label key is
configurable via `skha.acme_master_label`), SKHA treats it as the ACME
master: `state MASTER`, `priority 150`, **no health checks** - the VIP is
locked there so certificate renewal never gets interrupted by a VIP
migration. Every other manager is `state BACKUP` with a lower priority
(`skha.worker_base_priority`, default 80, minus 10 per worker index) and
runs the configured health checks; a failed check subtracts from its
priority (weight, default -10) until the VIP moves to the next-highest node.

If no node carries the label, all managers behave as plain BACKUP workers
and the highest-priority reachable node holds the VIP.

## Health checks are an instance variable

The upstream deployment this was extracted from named specific internal
services (a sync daemon, mail, Prometheus) directly in the keepalived
config. That does not belong in a published framework, so health checks are
now a list instance var, `skha.health_checks`, with a generic default that
checks nothing but the VIP's own webserver:

```yaml
skha:
  health_checks:
    - name: traefik
      ports: [80, 443]
      container_pattern: traefik-worker   # optional: skip the check where this
                                           # container isn't running (e.g. the
                                           # ACME master)
    - name: mail
      ports: [25, 587, 465, 143, 993]
    - name: sksync
      ports: [22000]
```

`name` must be a short lowercase identifier (letters, digits, underscore) -
it becomes a filename and a keepalived `vrrp_script` name. Each entry
renders its own `/usr/local/bin/skha_check_<name>.sh` from
`src/keepalived/scripts/skha_check.sh.j2`, and its own `vrrp_script` /
`track_script` block in `keepalived.conf.j2`. `interval`, `weight`, `fall`,
`rise` and `timeout` are all overridable per entry (defaults: 5s, -10, 2, 2,
5s).

## Required instance vars (vault)

There is deliberately no framework default for these - a VIP and a VRRP
password are estate-specific by construction, and the playbook fails
closed if they are missing:

```yaml
skha:
  vip: "203.0.113.10/24"        # REQUIRED: the floating VIP, CIDR notation
  auth_pass: "..."               # REQUIRED: VRRP auth password, same on every manager
  virtual_router_id: 51          # optional, default 51 - must be unique per VIP/broadcast domain
  interface: eth0                # optional, default: auto-detected primary interface
  advert_interval: 1             # optional, default 1 (seconds)
  worker_base_priority: 80       # optional, default 80
  acme_master_label: traefik.acme.master   # optional, default shown
  health_checks: [...]           # optional, see above; default is a single
                                  # generic check of ports 80/443 (traefik)
```

Unicast peers are **not** a vault var - they are calculated automatically
from `target_manager_group`'s inventory each run.

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/core/skha/deploy_skha-prod.yml \
  -e target_manager_group=<instance>-managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

Vault file: `<skstacks_vault_dir>/core/group_vars/<env>/skha-<env>_vault.yml`
(see the framework README's "Instance contract").

## Verifying failover

- **VIP location**: `ip -4 addr show <interface>` on each manager - exactly
  one should show the VIP as a secondary address.
- **Failover**: `systemctl stop keepalived` on the node currently holding the
  VIP, then re-run the `ip -4 addr show` check on the other managers; the VIP
  should reappear on the next-highest-priority reachable node within a few
  advertisement intervals. `systemctl start keepalived` to restore it (it
  will not preempt back automatically - `nopreempt` is set).
