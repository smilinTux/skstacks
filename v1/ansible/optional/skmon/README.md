# skmon

**Observability for a Swarm estate that works out of the box and alerts in under a minute: VictoriaMetrics for metrics, Loki for logs, Grafana, vmalert, Alertmanager and a small bridge that hands alerts to the estate's own `sk-alert`, fed by one Grafana Alloy agent per host.** Prometheus, node-exporter, cAdvisor, Promtail and Jaeger are retired. Optional extras: Pyroscope continuous profiling and a Proxmox exporter.

Every stateful service sits on one pinned node (`DATA_NODE`) on its local disk. Nothing on the alert path (agent, VictoriaMetrics, vmalert, Alertmanager, bridge) reads config from or writes data to shared storage, so a storage stall cannot silence the monitor.

## What is deployed

| Service | Role |
|---|---|
| `victoriametrics` | metric store and scraper (vmui behind forward auth) |
| `vmalert` | rule evaluation |
| `alertmanager` | routing, grouping, silences, inhibition (behind forward auth) |
| `bridge` | webhook receiver that runs the estate's `sk-alert`, plus `/healthz` |
| `loki` | log store |
| `grafana` | dashboards, signs in through Authentik OIDC |
| `skswarm-exporter` and `socket-proxy` | Swarm desired-versus-running state through a read-only Docker socket proxy |
| `pyroscope` | optional, profiles |
| `pve-exporter` | optional, Proxmox metrics (`PVE.enabled`) |

The Alloy agent is not a Swarm service. It is an Ansible-managed host container (or a systemd unit on a storage or hypervisor host) on every node, run by `agent.yml`. VictoriaMetrics scrapes each agent, so `up` gives node-down for free; logs are pushed by the agent to Loki.

The agent's `node` label is the inventory name. Inventory hostnames of swarm hosts must therefore equal their Swarm hostnames: `AgentMissing` and the node inhibitions join on `node`, and a mismatch makes them miss.

## Vault keys

All keys live under the `skmon:` map of the instance vault. Required keys have no default and the deploy fails with a message naming them. A key marked "retired" in the removed-keys section below fails the deploy if it is still set.

| Key | Default | Meaning |
|---|---|---|
| `CLUSTERNAME`, `DOMAIN` | required | Build the hostnames (`skmon[-env].<cluster>.<domain>`, `vmui[-env]...`, `alertmanager[-env]...`). |
| `ESTATE` | required | The `estate` label on every series and alert. |
| `DATA_NODE` | required | Swarm node that holds every stateful service. The deploy fails if it is unset or not a swarm node. |
| `DATA_PATH` | `/srv/local/skmon-<env>` | Data directory on `DATA_NODE`. Must be local disk and must not be under `/var/data`. Never deleted by parking. |
| `GRAFANA_ADMIN_USER`, `GRAFANA_ADMIN_PASSWORD`, `GRAFANA_SECRET_KEY` | required | Break-glass local Grafana admin and the signing key. |
| `GRAFANA_ROOT_URL` | `https://skmon[-env].<cluster>.<domain>` | Grafana's public URL, no trailing slash. |
| `GRAFANA_OIDC_CLIENT_ID`, `GRAFANA_OIDC_CLIENT_SECRET` | required, 32+ characters each | Authentik OIDC client for Grafana. |
| `AUTHENTIK_HOST` | required | Public hostname of sksso. Grafana's OIDC endpoints are built from it. Also adds an `authentik` health probe. |
| `AUTHENTIK_PROVISION` | `true` | The play creates the Authentik provider, applications, bindings and tiles. `false` skips that (create them by hand) and also skips retiring the old tiles. |
| `AUTHENTIK_TILE` | `true` | Create the portal tiles. |
| `AUTHENTIK_VAULT_FILE` | `sksso-...` beside this vault | sksso's vault, read for `sksso.api_token` (32+ characters). |
| `AUTHENTIK_INTERNAL_URL` | `http://sksso-<env>_server:9000` | Where the provisioner reaches Authentik. |
| `AUTHENTIK_NETWORK` | `sksso-<env>` | Overlay network the provisioner joins. |
| `PROVISIONER_IMAGE` | pinned image | Image of the one-shot Authentik provisioner. |
| `FORWARD_AUTH_MIDDLEWARE` | `authentik@file` | Traefik middleware protecting vmui and Alertmanager. Also marks probe targets `forward_auth`. |
| `METRICS_RETENTION` | `90d` | VictoriaMetrics retention. |
| `LOGS_RETENTION` | `30d` | Loki retention. |
| `PROFILES_RETENTION` | `14d` | Pyroscope retention. Both retention flags the image needs are rendered from this one key. |
| `SCRAPE_INTERVAL_FAST` | `15s` | Interval for host and swarm signals. |
| `SCRAPE_INTERVAL` | `30s` | Interval for container and app metrics. |
| `AGENT_PORT` | `12345` | Port of each node's Alloy agent. |
| `AGENT_LISTEN_ADDR` | inventory address | Map of node name to the IPv4 address the agent listens on and VictoriaMetrics scrapes. |
| `HEALTHZ_ALLOW` | required | List of IPv4 addresses or CIDRs of the outside monitor allowed to reach `/healthz`. Nothing else is allowed. |
| `HEALTHZ_PORT` | `9095` | Host-mode port on `DATA_NODE` serving only `/healthz`. |
| `LOKI_PUSH_PORT` | `3100` | Host-mode port on `DATA_NODE` where agents push logs, allowed from estate hosts only. |
| `PYROSCOPE_PORT` | `4040` | Pyroscope port, allowed from profiling agents only. |
| `PROBES` | every Traefik router host | Blackbox targets, a list of `{name, url, severity}`. Setting it replaces discovery. |
| `PROFILING` | `{enabled: false, hosts: []}` | Continuous profiling, `{enabled, hosts: [...]}`. See Install modes. |
| `PVE` | `{enabled: false}` | Proxmox metrics, `{enabled, targets: [...], verify_ssl}`. `verify_ssl` defaults to `true`. Needs `PVE_TOKEN_ID`, `PVE_TOKEN_SECRET` and a non-empty `targets`. |
| `PVE_TOKEN_ID`, `PVE_TOKEN_SECRET` | empty | Proxmox API token, `user@realm!tokenname` and its value. Rendered into a Docker secret. |
| `PVE_UID`, `SKSWARM_UID`, `BRIDGE_UID` | `65534` | Container user ids; `BRIDGE_UID` also owns `DATA_PATH/bridge`. |
| `ALERT_ROUTING` | required | Map of `NAME: value` rendered into the bridge's `alert.env` (Docker secret). |
| `ALERT_SECRETS` | required | Map of `NAME: value` rendered into `alert.secret` (Docker secret). |
| `ALERT_TALK` | `false` | Pass `--talk` to `sk-alert`. There is no `--talk` unless this is true. |
| `ALERT_OUT_OF_DOMAIN_CHANNEL` | required unless the next key is set | Your declaration, `telegram` or `talk-external`, that the estate's critical route (in `ALERT_ROUTING` and `ALERT_SECRETS`) includes a channel that does not run on the monitored estate. The deploy guard checks only that this value is one of those two (or that in-domain only is accepted); it does not inspect the routing or send anything. |
| `ALERT_ACCEPT_IN_DOMAIN_ONLY` | `false` | Accept in-domain delivery only. Needs the reason below. |
| `ALERT_ACCEPT_IN_DOMAIN_ONLY_REASON` | empty | Why in-domain only is acceptable; required when the key above is true. |
| `SKALERT_SCRIPT_SRC` | required | Path of the estate's own `sk-alert` on the controller. A relative path resolves against the directory of the vault file; an absolute path or `~/...` is used as given. |
| `SKALERT_SCRIPT_SHA256` | required | Pinned sha256 of that script. The deploy fails if the file differs. |
| `D_STATE_THRESHOLD` | `10` | Processes in uninterruptible sleep that raise the storage D-state alert. |
| `EXTRA_SCRAPE_CONFIGS` | `[]` | Extra VictoriaMetrics scrape jobs, rendered as given. |
| `TRAEFIK_SCRAPE_NAME` | `tasks.skfence-<env>_traefik` | Swarm DNS name of the Traefik stack service scraped for router metrics. |
| `GRAFANA_PLUGINS` | `[]` | Grafana plugin ids to install. Current Grafana refuses the Angular ones. |
| `GRAFANA_HEALTH_START_PERIOD` | `60s` | Grafana health start period; raise it on slow shared filesystems. |
| `SMTP_ENABLED`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM_ADDRESS`, `SMTP_FROM_NAME`, `SMTP_SECURE` | disabled | Grafana mail. `SMTP_FROM_NAME` defaults to `Grafana`, `SMTP_SECURE` to `true`. |
| `INSTANCE`, `APP_ENV` | `skmon-<env>`, `<env>` | Instance and environment names used in the env files. |
| `networks` | the stack defaults | Overlay networks the stack joins. |
| `replicas` | `1` | `0` (or the string "0") parks the stack. See Parking. |
| `VICTORIAMETRICS_IMAGE`, `VICTORIAMETRICS_CPU_LIMIT`, `VICTORIAMETRICS_CPU_RESERVATION`, `VICTORIAMETRICS_MEMORY_LIMIT`, `VICTORIAMETRICS_MEMORY_RESERVATION` | pinned image, `1.0`, `0.1`, `1G`, `256M` | Image and resources. |
| `VMALERT_IMAGE`, `VMALERT_CPU_LIMIT`, `VMALERT_CPU_RESERVATION`, `VMALERT_MEMORY_LIMIT`, `VMALERT_MEMORY_RESERVATION` | pinned image, `0.25`, `0.05`, `128M`, `32M` | Image and resources. |
| `ALERTMANAGER_IMAGE`, `ALERTMANAGER_CPU_LIMIT`, `ALERTMANAGER_CPU_RESERVATION`, `ALERTMANAGER_MEMORY_LIMIT`, `ALERTMANAGER_MEMORY_RESERVATION` | pinned image, `0.25`, `0.1`, `128M`, `32M` | Image and resources. |
| `BRIDGE_IMAGE`, `BRIDGE_CPU_LIMIT`, `BRIDGE_CPU_RESERVATION`, `BRIDGE_MEMORY_LIMIT`, `BRIDGE_MEMORY_RESERVATION` | pinned image, `0.1`, `0.05`, `64M`, `16M` | Image and resources. |
| `LOKI_IMAGE`, `LOKI_CPU_LIMIT`, `LOKI_CPU_RESERVATION`, `LOKI_MEMORY_LIMIT`, `LOKI_MEMORY_RESERVATION` | pinned image, `1.0`, `0.1`, `1G`, `384M` | Image and resources. |
| `GRAFANA_IMAGE`, `GRAFANA_CPU_LIMIT`, `GRAFANA_CPU_RESERVATION`, `GRAFANA_MEMORY_LIMIT`, `GRAFANA_MEMORY_RESERVATION` | pinned image, `0.5`, `0.1`, `512M`, `128M` | Image and resources. |
| `SKSWARM_EXPORTER_IMAGE`, `SKSWARM_EXPORTER_CPU_LIMIT`, `SKSWARM_EXPORTER_CPU_RESERVATION`, `SKSWARM_EXPORTER_MEMORY_LIMIT`, `SKSWARM_EXPORTER_MEMORY_RESERVATION` | pinned image, `0.05`, `0.05`, `64M`, `16M` | Image and resources. |
| `SOCKET_PROXY_IMAGE`, `SOCKET_PROXY_CPU_LIMIT`, `SOCKET_PROXY_CPU_RESERVATION`, `SOCKET_PROXY_MEMORY_LIMIT`, `SOCKET_PROXY_MEMORY_RESERVATION` | pinned image, `0.05`, `0.05`, `64M`, `16M` | Image and resources. |
| `PYROSCOPE_IMAGE`, `PYROSCOPE_CPU_LIMIT`, `PYROSCOPE_CPU_RESERVATION`, `PYROSCOPE_MEMORY_LIMIT`, `PYROSCOPE_MEMORY_RESERVATION` | pinned image, `0.5`, `0.1`, `1G`, `256M` | Image and resources. |
| `PVE_EXPORTER_IMAGE`, `PVE_EXPORTER_CPU_LIMIT`, `PVE_EXPORTER_CPU_RESERVATION`, `PVE_EXPORTER_MEMORY_LIMIT`, `PVE_EXPORTER_MEMORY_RESERVATION` | pinned image, `0.1`, `0.05`, `128M`, `32M` | Image and resources. |
| `ALLOY_IMAGE` | pinned image | Image of the per-host agent container. |
| `skmon_extra_hosts` | none | Not a vault key: an inventory group of non-swarm hosts (storage, hypervisor) that also run an agent. Members default to systemd mode and light mode. |

Per-host inventory variables for the agent: `agent_mode` (`container` or `systemd`), `agent_light` (true for `skmon_extra_hosts` members), `skmon_role` (`storage` marks a storage host, default `swarm`).

The image defaults are the digest-pinned set in `src/config/skmon/pins.yml`. Override one only with an exact version and digest, never a floating tag.

Minimal vault, every key the deploy requires when unparked (placeholders; the render gate uses `v1/tests/render/vars/skmon.example.yml`):

```yaml
skmon:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.test"
  ESTATE: "<estate label>"
  DATA_NODE: "<swarm node hostname>"
  GRAFANA_ADMIN_USER: "admin"
  GRAFANA_ADMIN_PASSWORD: "<placeholder>"
  GRAFANA_SECRET_KEY: "<placeholder>"
  GRAFANA_OIDC_CLIENT_ID: "<placeholder, 32+ characters>"
  GRAFANA_OIDC_CLIENT_SECRET: "<placeholder, 32+ characters>"
  AUTHENTIK_HOST: "sso.<cluster>.example.test"
  HEALTHZ_ALLOW: ["<outside monitor IPv4>"]
  SKALERT_SCRIPT_SRC: "sk-alert"
  SKALERT_SCRIPT_SHA256: "<sha256 of that script>"
  ALERT_ROUTING: {TELEGRAM_CHAT_ID: "<placeholder>"}
  ALERT_SECRETS: {TELEGRAM_BOT_TOKEN: "<placeholder>"}
  ALERT_OUT_OF_DOMAIN_CHANNEL: telegram
```

## Removed keys

`AUTHENTIK_TILE_GROUP` and `GRAFANA_BASIC_AUTH` are retired. Grafana signs in through OIDC, and vmui and Alertmanager use forward auth through `FORWARD_AUTH_MIDDLEWARE` (default `authentik@file`). Every `PROMETHEUS_*`, `NODE_EXPORTER_*`, `CADVISOR_*`, `PROMTAIL_*` and `JAEGER_*` key is gone and the deploy fails with a message naming any that is still set: delete them.

On the Authentik applications skmon owns, `skadmin` is the only binding. Any other binding is removed on every deploy; admin access is through the `skadmin` group. The retired `jaeger` and `prometheus` applications are removed only when `AUTHENTIK_PROVISION` is true. Otherwise remove them by hand.

## Install modes

The agent runs in one of two modes, chosen per host with the `agent_mode` inventory variable.

- **container** (the default for swarm nodes): a host container with host network and host PID, created and replaced by `agent.yml` only when its config hash changes. Never `--privileged`.
- **systemd** (the default for `skmon_extra_hosts`): a unit on storage and hypervisor hosts, where a container would be the wrong thing to add. With `agent_light` (the default there) it collects only systemd, host, ZFS, SMART and NFS metrics, at a 60 s interval, with no log shipping and no profiling, under `MemoryMax=256M` and `CPUQuota=5%`. Measure its cost for a day after install and remove it with `state=absent` if it shows on the host.

Profiling (`PROFILING.enabled` with `PROFILING.hosts`) runs only on container-mode agents. Those agents get the capabilities BPF, PERFMON, SYS_ADMIN and SYS_PTRACE and a read-only tracefs mount, and are never privileged. Kernels older than 5.11 also need SYS_RESOURCE, which is not added. A host in `PROFILING.hosts` that is not a container-mode agent fails the deploy.

## Data node and storage

`DATA_NODE` is the node that holds every stateful skmon service. `DATA_PATH` defaults to `/srv/local/skmon-<env>` and must not be under `/var/data`; the deploy fails if it is, or if `DATA_NODE` is unset. The play creates the directories on `DATA_NODE`.

| Path under `DATA_PATH` | Owner | Contents |
|---|---|---|
| `vm/` | 0:0 | VictoriaMetrics data |
| `loki/` | 10001 | Loki chunks, TSDB index, compactor, ruler |
| `grafana/` | 472 | Grafana database (sessions, user prefs, annotations) |
| `alertmanager/` | 65534 | silences and notification log |
| `bridge/` | `BRIDGE_UID` | `sk-alert` de-dup state and thread table |
| `pyroscope/` | 10001 | profiles |

Configs for every Swarm service are Swarm configs (stored in Raft, available while shared storage is stalled), named with a content hash so a change rolls the service. Grafana's provisioning directory is rendered into `DATA_PATH/grafana-provisioning` instead. Dashboard JSON is pruned to the shipped tree on each deploy, so a dashboard added by hand under that directory does not survive; save such a board in Grafana's own database. The Proxmox board is built here and copied only with `PVE.enabled`.

**The backing store of `DATA_NODE`'s local disk matters.** When the swarm nodes are VMs, their "local" disks may sit on the same hypervisor pool that backs the shared export. A pool suspension then freezes the monitor's disk too. Put `DATA_PATH` on a disk backed by a different pool where one exists. Where none exists (a single-host estate), the outside view in the dead-man section below is the only guaranteed signal for a whole-host failure, and the estate's runbook must say so.

Give each stack its own dedicated virtual disk on `DATA_NODE`, never on the estate's shared storage, and do not add the disk to a storage pool that is mid-migration. Sharing the root-disk pool is acceptable when that pool is redundant and has I/O headroom: skmon writes a few MB/s. A single-device pool is acceptable, because monitoring data can be rebuilt.

A firewall on every agent host and on `DATA_NODE` restricts the agent, Loki push, Pyroscope and `/healthz` ports to the addresses that need them. It is one iptables chain, `SKMON-<ENV>`, rebuilt atomically and restored at boot by a oneshot systemd unit. It is IPv4 only: sources must be IPv4 literals or CIDRs, and the play refuses a name.

## Alert routing

vmalert evaluates the rules, Alertmanager groups and inhibits, and the bridge runs the estate's own pinned `sk-alert` (`SKALERT_SCRIPT_SRC`, checked against `SKALERT_SCRIPT_SHA256` on every deploy). Its secrets are Docker secrets (`alert.env` from `ALERT_ROUTING`, `alert.secret` from `ALERT_SECRETS`), written shell-quoted, so a value with spaces or quotes is safe. `--talk` is passed only when `ALERT_TALK` is true. Delivery is `sk-alert`'s own routing, built from `ALERT_ROUTING` and `ALERT_SECRETS`, for the level the bridge passes; the bridge itself routes nothing to a particular channel. `ALERT_OUT_OF_DOMAIN_CHANNEL` is the operator's declaration that the estate's critical route includes a channel outside the estate's domain (`telegram` or `talk-external`), so storage alerts still get out when shared storage or the domain is down. The deploy guard only checks the declaration is one of those two values, or that `ALERT_ACCEPT_IN_DOMAIN_ONLY` is true with a written reason; it cannot see your `sk-alert` configuration, so keep the two in agreement.

- A drained node raises a 30 m warning (`SwarmNodeNotReady`), and only critical node alerts (`NodeDown`, a critical `SwarmNodeNotReady`) mute the other alerts on that node, so a drain does not hide that node's storage, NFS or D-state alerts.
- A firing storage alert mutes its consequences estate-wide; Authentik down mutes the probes of targets behind forward auth; a critical mutes the warning for the same alert and target.
- `SwarmApiUnresponsive` also fires when `skswarm-exporter` is down or absent, so a dead exporter is never read as a healthy swarm.

## Dead-man's switch

`Watchdog` always fires and is routed only to the bridge's `/watchdog` path, with a one minute repeat. The bridge's `/healthz` returns 200 only if the last Watchdog arrived less than 3 minutes ago and the last delivery did not fail. An outside Uptime Kuma keyword monitor probes it, so if VictoriaMetrics, vmalert, Alertmanager or the bridge dies, or `DATA_NODE` goes down, the outside monitor raises it through its own channel.

The bridge publishes `HEALTHZ_PORT` in host mode on `DATA_NODE`, serving only `/healthz`, reachable only from `HEALTHZ_ALLOW`. There is no Traefik router on it, so the edge is not on the dead-man path. The monitor is created by hand per estate, and the Kuma doing the probing must not share the monitored swarm's failure domain (shared storage, network, hypervisor). An estate is not done until that outside probe is green.

## Parking

`replicas: 0`, or the string "0", parks the stack. Every Swarm service renders scaled to 0, and the agent play stops and removes the agent on every host. The deploy script skips health waits and forced updates, and the guards that only make sense unparked (the Authentik provisioning and the probe discovery, for example) are skipped. `DATA_PATH` and the agent's state directory are never deleted, so unparking brings back history.

## Sizing

Active series is roughly: `nodes x 1,500 (host) + containers x 60 (cadvisor, after drops) + services x 8 (swarm) + probes x 15 + Traefik routers x 60 + about 8,000 (self-monitoring)`. At a 15 to 30 s interval VictoriaMetrics stores well under 1 byte per sample; budget 1 byte.

A five-node swarm with about 90 running tasks, 80 services and 40 probed endpoints comes to about 25,000 to 35,000 active series, about 1,000 to 1,500 samples per second.

| Component | Memory reservation / limit | CPU limit | Disk at 30 d | Disk at 90 d |
|---|---|---|---|---|
| Alloy agent, per node | 128M / 384M (640M with profiling) | 0.5 | under 1 GB WAL and positions | same |
| VictoriaMetrics | 256M / 1G | 1.0 | 2 to 4 GB | 6 to 11 GB |
| vmalert | 32M / 128M | 0.25 | none | none |
| Alertmanager | 32M / 128M | 0.25 | under 100 MB | same |
| skmon-bridge | 16M / 64M | 0.1 | under 100 MB | same |
| skswarm-exporter + socket proxy | 32M / 128M | 0.1 | none | none |
| Loki | 384M / 1G | 1.0 | 2 to 6 GB (depends on log volume) | 6 to 18 GB if raised to 90 d |
| Grafana | 128M / 512M | 0.5 | under 1 GB | same |
| Pyroscope (optional) | 256M / 1G | 0.5 | 1 to 3 GB at 14 d | not recommended |
| **Total on DATA_NODE** | about 1.2 GB reserved, 4 GB limit | under 0.3 cores average | **plan 15 GB** | **plan 40 GB** |

Loki volume is the one number to measure on each estate. A 64 GB dedicated disk covers 30 d of metrics and 14 d of logs with headroom.

## skhealth stays

`skhealth` runs from a systemd timer on every node with no container, no swarm and no shared-storage dependency, and already checks NFS mounts, ZFS pools and swarm state every 5 minutes. It stays deployed as the second, independent backstop (D19). skmon is the fast path; skhealth is the one that still works if Docker itself is wedged on the node. skmon does not replace it.

## Migration from skmon v1

1. Render with the instance vault and diff against what is running (the render gate's layer 3, `v1/tests/render/render_playbooks.py --service skmon`).
2. Set `DATA_NODE` and `DATA_PATH`: a dedicated virtual disk on `DATA_NODE`, never on shared storage, and not on a pool that is mid-migration. The play creates the directories.
3. Deploy. `docker stack deploy --prune` removes `prometheus`, `node-exporter`, `cadvisor`, `promtail` and `jaeger`. The first v2 deploy restarts every skmon service and replaces the stack. Old data on `/var/data` is untouched.
4. Optional: copy the old Grafana database to `DATA_PATH/grafana` once, if anyone cares about saved prefs and annotations. Dashboards are provisioned, so nothing is lost without it.
5. Prometheus history is not migrated. `vmctl` can import it if it is ever wanted.
6. The retired Authentik tiles are removed by the play when `AUTHENTIK_PROVISION` is true (otherwise by hand). After a week, remove the old `/var/data/skmon-<env>` directories by hand.
7. Verify: every node `up`, a test alert end to end (run on `DATA_NODE`, see Verify: `amtool alert add` of a `severity=warning` alert, seen in the room, then resolved), and an outside Kuma keyword monitor on `/healthz`, created by hand, green.

An estate is not done until the outside probe is green. On a single-host estate that probe is the only guaranteed signal for a whole-host failure, because the monitor shares the host's failure domain; the estate's runbook must say so.

## Verify

```bash
# Targets: open vmui (behind forward auth) and query `up`: every node is 1.

# Rules and routing: a test alert, from inside the Alertmanager container on DATA_NODE.
AM=$(docker ps -q -f name=skmon-<env>_alertmanager)
docker exec "$AM" amtool alert add SkmonTest severity=warning estate=<estate label> \
  --annotation=summary="skmon test" --alertmanager.url=http://localhost:9093
# Seen in the room. Resolve it by re-adding it with an end time in the past or now:
docker exec "$AM" amtool alert add SkmonTest severity=warning estate=<estate label> \
  --end="$(date -u +%Y-%m-%dT%H:%M:%SZ)" --alertmanager.url=http://localhost:9093

# The dead-man path, from the outside monitor's address.
curl -fsS "http://<DATA_NODE>:<HEALTHZ_PORT>/healthz"
```

Health endpoints per component: `/health` VictoriaMetrics, `/ready` Loki, `/-/healthy` Alertmanager, `/api/health` Grafana. Offline checks: `python3 -m pytest -q v1/tests/test_skmon*.py` and `python3 v1/tests/render/render_playbooks.py --service skmon`.

## Backups

Alertmanager silences and the Grafana database are small and worth keeping: back up `DATA_PATH/grafana` and `DATA_PATH/alertmanager`. VictoriaMetrics and Loki data can be lost without breaking anything.
