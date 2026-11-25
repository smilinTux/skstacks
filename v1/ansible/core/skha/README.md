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

## Failover: every manager is a backup, the VIP follows a healthy worker

Every manager is `state BACKUP` with `nopreempt` and tracks the health
checks. The VIP sits on one node and moves only when that node fails a VIP
gate check or stops answering VRRP. A node that recovers does not take the
VIP back (`nopreempt`), so the VIP does not flap.

- **Priority is only a preference.** A node carrying the label
  `traefik.acme.master=true` (key configurable via `skha.acme_master_label`)
  gets `skha.acme_master_priority` (default 150), so it wins the election
  when every node starts together. The others get `skha.worker_base_priority`
  (default 80) minus 10 per index.
- **VIP gate checks decide where the VIP may live.** The checks named in
  `skha.vip_gate_checks` (default `[traefik]`) default to `require: true`
  and `weight: 0` and start failed (`init_fail`). With weight 0, `fall`
  failures put the node in FAULT and the VIP moves to a peer: about 10 s at
  the defaults (interval 5, fall 2) plus the peers' master-down timer (about
  3 s). A check that hits its timeout counts as a failure, so a hung dockerd
  also moves the VIP. `init_fail` stops a node with no worker from winning
  the first election when every node boots together. With `require: true`, a node with no running
  `traefik-worker` FAILS the check, so the VIP can never sit on a node that
  answers nothing on 80/443. Either default can be overridden per entry.
- **The gate is chosen by check NAME.** If your `skha.health_checks` calls
  the Traefik check something other than `traefik`, set `vip_gate_checks`
  to that name, or the VIP will never move on Traefik health.
- **Redeploys restart keepalived one manager at a time.** Each restart waits
  until that node reports MASTER or BACKUP (`skha_notify.sh` writes
  `/run/skha.state`) before the next node restarts, so the managers never all
  drop out together. Restarting the node that holds the VIP still moves it, a
  hand-off of about a second. A node that is not ready within
  `skha.restart_settle_seconds` fails the handler and stops the rollout there
  (for example, a manager with no Traefik worker stays FAULT), so the next
  manager is not restarted while no node can take the VIP. The check runs
  only when the config changes. The default wait is the slowest check's
  `interval * rise + timeout` plus 15 s (30 s for the default check).
- **Gate names are validated.** `vip_gate_checks` must be a list, and every
  name in it (including the default `[traefik]`) must match a `health_checks`
  entry, or the deploy stops before touching any node.
- **A network partition** can still give two masters, as with any VRRP. When
  it heals, the higher-priority node keeps the VIP (VRRP master-to-master
  resolution, unaffected by `nopreempt`).
- **A negative weight never moves the VIP on its own.** Under `nopreempt` a
  master whose priority drops KEEPS the VIP. Weighted checks (default -10)
  only rank the nodes for the next election.

Why the ACME master is not locked any more: skfenceha's ACME uses the
Cloudflare DNS-01 challenge, which needs no inbound traffic, so renewal does
not care where the VIP is. The old lock (`state MASTER`, priority 150, no
health checks) let the VIP sit on a node with no Traefik worker. On
2026-11-19 one estate's ACME-master manager held the VIP while its worker could not start (its
NFS `/var/data` did not mount after a host crash), so public 443 was refused
and the two healthy workers never got the VIP. An estate whose ACME
challenge does need inbound traffic can set `skha.lock_vip_to_acme_master:
true` to restore the lock, knowing the VIP then has no failover.

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
      container_pattern: traefik-worker   # a VIP gate check by default:
                                           # require: true, weight: 0
    - name: mail
      ports: [25, 587, 465, 143, 993]
      container_pattern: mail-worker
      require: true   # optional, default false: FAIL (not skip) when the
                       # container is absent. Use when a check's container is
                       # expected to always be present on this node, so the
                       # VIP fails over if it dies instead of the check
                       # silently passing.
    - name: sksync
      ports: [22000]
```

`name` must be a short lowercase identifier (letters, digits, underscore) -
it becomes a filename and a keepalived `vrrp_script` name. Each entry
renders its own `/usr/local/bin/skha_check_<name>.sh` from
`src/keepalived/scripts/skha_check.sh.j2`, and its own `vrrp_script` /
`track_script` block in `keepalived.conf.j2`. `interval`, `weight`, `fall`,
`rise` and `timeout` are all overridable per entry (defaults: 5s, -10, 2, 2,
5s; a VIP gate check's weight defaults to 0 instead of -10).

## Services with host-published ports follow the VIP

A service that publishes its ports in host mode (skmail: 25, 465, 587, 143,
993) answers only on the node running its task, so it must run where the
VIP is. Pin the service to a node label and list that label in
`skha.vip_node_labels`:

```yaml
skha:
  vip_node_labels: [mail-vip]   # skmail is constrained to node.labels.mail-vip == true
```

Whenever a node becomes MASTER, `skha_notify.sh` runs
`skha_vip_labels.sh` in the background. It adds each label to that node,
then removes it from every other node, and Swarm moves the service onto the
new VIP holder (its old task is rejected with "assigned node no longer
meets constraints"). Every deploy also runs it once on the current VIP
holder, which fixes an estate where the label and the VIP are already on
different nodes. Moves are logged to `/var/log/keepalived-notify.log` and
syslog (`skha-vip-labels`).

The service's data has to be reachable from every manager (shared storage),
because it can now start on any of them. Without `vip_node_labels`, nothing
moves the label, and the VIP can sit on a node where those ports refuse
every connection.

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
  acme_master_priority: 150      # optional, default 150 (a preference, not a lock)
  vip_gate_checks: [traefik]     # optional, default shown: require + weight 0 + init_fail
  vip_node_labels: []            # optional: Swarm node labels that follow the VIP, e.g. [mail-vip]
  restart_settle_seconds: 30     # optional, default derived from the checks: max per-node wait for MASTER/BACKUP
  lock_vip_to_acme_master: false # optional, default false: true = old lock, no failover
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
