# sknet operations

Day-two procedures for sknet: deploy order, verification, adding an exporter or a site, retention and disk, upgrade, removal and troubleshooting. Commands use placeholders (`<env>`, `<interface>`) and example addresses from 192.0.2.0/24. The framework is assumed checked out as `framework/` in your instance repo; adjust paths to yours.

## Prerequisites

- A swarm node for the data (`DATA_NODE`) with **AVX2** (`grep -w avx2 /proc/cpuinfo`), local disk for `DATA_DIR` (not NFS), and room for `DISK_BUDGET_GB` (default 20).
- Traefik with a host-mode entrypoint that sees real client addresses, and Authentik forward auth if you keep `AUTH_MODE: authentik`.
- Exporter hosts on `apt` (softflowd is installed with apt when missing; other package managers need softflowd installed by hand).
- Docker Engine on the live host (the live play does not install it).

## Vault and inventory

The vault holds one `sknet:` block; only what you change from the defaults is needed:

```yaml
sknet:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  DATA_NODE: "<swarm node hostname>"
  GATE_SECRET: "<random, 32+ characters: openssl rand -hex 32>"
  FLOW_SOURCES: ["192.0.2.10", "198.51.100.7"]
  ALLOW_CIDRS: ["192.0.2.0/24", "100.64.0.0/10"]
  LIVE_UPSTREAM: "http://192.0.2.30:3000"     # optional, enables the live router
```

`GATE_SECRET` is required (at least 32 characters of letters, digits, dash or underscore). It is the value Traefik sends in the `X-Sknet-Gate` header and the gate requires; see [architecture](./architecture.md#header-trust-and-the-gate). `DATA_NODE` must be both the swarm node hostname and a host name the deploy's inventory can reach: the data directories and the flow firewall are created on it with `delegate_to`, so a hostname that is not an inventory host fails the deploy.

The exporter inventory is an instance-private ini (not in the framework):

```ini
[sknet_exporter_hosts]
gateway-1  ansible_host=192.0.2.10
site-a     ansible_host=198.51.100.7

[sknet_exporter_hosts:vars]
sknet_collector=192.0.2.20

[sknet_live_hosts]
gateway-1
```

Per-host values go in `host_vars/<host>.yml`:

```yaml
# host_vars/gateway-1.yml
sknet_export_address: "192.0.2.10"       # the address the collector sees as source
sknet_interfaces:
  - { name: lan0,   index: 101, class: lan,         speed_mbps: 1000, description: "Site LAN" }
  - { name: mesh0,  index: 102, class: mesh }
  - { name: link-b, index: 103, class: estate-link }
sknet_live_interfaces: [lan0, mesh0]
sknet_live_bind: "192.0.2.10"
sknet_live_allow_sources: ["192.0.2.0/24"]
```

Interface rules, enforced by both the deploy and the host play: name 1 to 15 characters of letters, digits, `_`, `.`, `-`; index an integer from 100 to 65535, unique per host; class one of `lan`, `mesh`, `estate-link`, `internet`.

## Deploy order

1. **Stack.** Deploy the collector first so the ports and the metadata exist before flows arrive. Pass the exporter inventory so the static metadata is built from it:

   ```bash
   ansible-playbook -i envs/<env>/inventory.ini -i shared/sknet_exporters.ini \
     framework/v1/ansible/optional/sknet/deploy_sknet-<env>.yml \
     -e target_manager_group=swarm_managers \
     --vault-password-file ~/.vault_pass_env/.<instance>_<env>_vault_pass
   ```

   It validates the vault, creates `DATA_DIR` on `DATA_NODE`, checks the disk, renders the stack, installs the flow port allowlist, creates the overlay networks and runs `docker stack deploy`. Without `-i <exporters ini>` it uses vault `STATIC_EXPORTERS` instead, and refuses to continue if neither names an exporter.

2. **Exporters.** Start the flow sources, one host at a time with `-l` if you like:

   ```bash
   ansible-playbook -i shared/sknet_exporters.ini \
     framework/v1/ansible/optional/sknet/exporters.yml -l gateway-1
   ```

3. **Live view** (optional), after the stack, because the proxy points at it:

   ```bash
   ansible-playbook -i shared/sknet_exporters.ini \
     framework/v1/ansible/optional/sknet/live.yml -l gateway-1
   ```

   Set `LIVE_UPSTREAM` in the vault and redeploy the stack to add the live host to the gate. Run the deploy with the live hosts in the inventory (`-i <ini>` with `sknet_live_hosts`) so it keeps the gate off the live host and checks `LIVE_UPSTREAM` against `sknet_live_bind`.

4. Verify (next section).

For another environment pass `-e env=<env>` to the host plays; they read the same environment's vault (default `prod`).

## Verify

On the collector node:

```bash
docker service ls --filter name=sknet-<env>          # all replicas 1/1
sudo iptables -L SKNET-<ENV> -n -v                   # one RETURN per FLOW_SOURCES address, then DROP, per port
sudo tcpdump -ni any -c 5 udp port 2055              # flow packets arriving
```

On an exporter host:

```bash
systemctl status 'sknet-softflowd@*'                 # one active instance per interface
cat /etc/sknet/softflowd-<interface>.env             # collector, port, interface index
```

In the console, open `https://sknet.<cluster>.example.com/`. The home page shows the last flow once an exporter is sending; the table view grouped by exporter and interface shows your interface names. If you see an interface index instead of a name, that index is not in the exporter's `sknet_interfaces`.

Count rows in ClickHouse:

```bash
docker exec $(docker ps -q -f name=sknet-<env>_clickhouse) \
  clickhouse-client -q "SELECT count(), max(TimeReceived) FROM flows"
```

## Add an exporter

1. Add the host to `sknet_exporter_hosts` and give it `sknet_export_address`, `sknet_collector` and `sknet_interfaces`.
2. Add its `sknet_export_address` to the vault's `FLOW_SOURCES`. The deploy refuses a host that is missing from the list.
3. **Redeploy the stack** (step 1 above). This rebuilds the Akvorado metadata and the port allowlist; both are read at deploy time.
4. Run `exporters.yml` for the host. The play loads the vault, checks the collector (an IPv4 literal; a public address is refused unless `sknet_allow_public_collector: true`), installs softflowd if needed, masks the distribution's own `softflowd.service` so a flow never leaves twice, and starts one instance per interface.

To change an interface list, edit the hostvars and rerun both plays. Instances for interfaces no longer listed are stopped, disabled and their env file removed.

To remove a host from sending, set `sknet_state: absent` on it and run `exporters.yml`. It removes the instances, unit, env files and the mask it set, and leaves the softflowd package installed.

## Add a site

A site is an exporter on a host that is on the far side of a link you want to see (or a remote host you do not fully trust).

1. Pick an exporter address the collector will see. If the site is behind NAT, the source address the collector sees is the translated one, so use that address (see troubleshooting).
2. Add the host as an exporter (above). Use `internet` for its public interface and `mesh` or `estate-link` for tunnels.
3. If the site is one-way (it may not open connections into your network), follow the telemetry exception in the [multi-hop pattern](multi-hop-pattern.md): one UDP port to one collector, mesh policy plus the host egress guard.
4. Redeploy the stack, run `exporters.yml` for the site host, and verify that its exporter name appears in the console.

## Retention and disk

Every TTL derives from two vault knobs:

| Knob | Default | Covers |
|---|---|---|
| `FLOW_TTL_DAYS` | 30 | raw table and the 1 minute table |
| `FLOW_AGG_TTL_DAYS` | 90 (max 365) | 5 minute table; the hourly table keeps 4 times as long |

Decide before the first start. ClickHouse sizes partitions from the TTL when it creates the tables, so a large change later does not take full effect.

**Sizing.** A raw row is about 100 to 150 bytes compressed:

```
GB per day = flows per second x 86400 x ~125 bytes / 1e9
```

At 100 flows/s that is about 8.6 million rows and roughly 1 to 1.5 GB a day, so 20 GB holds about 15 to 20 days of raw flows. The aggregate tables are small. Measure instead of guessing:

```sql
SELECT formatReadableSize(sum(bytes_on_disk)) FROM system.parts
WHERE database = 'default' AND active;
```

Set `FLOW_TTL_DAYS` so the raw table fits in `DISK_BUDGET_GB`.

**Disk gate.** `DISK_BUDGET_GB` is a planning figure, not a quota. Before touching the stack the deploy measures `DATA_DIR` on `DATA_NODE` and refuses to continue when *what `DATA_DIR` already uses plus what is free* is below `DISK_BUDGET_GB`. A redeploy of a growing install therefore passes while a fresh, too small disk is refused. The message names the disk and the numbers. Free space, point `DATA_DIR` at a bigger local disk, or lower `DISK_BUDGET_GB` together with `FLOW_TTL_DAYS`.

**Floor.** `MIN_FREE_GB` (default 5) is the hard stop. ClickHouse stops writing when the disk has less free than that, before the disk fills. A TTL bounds bytes only roughly, which is why the floor exists. When it trips, flows are lost until space returns: reduce the TTL and free space.

Kafka is only a buffer: 12 hours of retention, 2 partitions and a size cap of one twentieth of `DISK_BUDGET_GB` per partition.

## Upgrade

Images are pinned by tag (or digest); there is no `latest`.

1. Bump the image in the vault (`AKVORADO_IMAGE`, `CLICKHOUSE_IMAGE`, `KAFKA_IMAGE`, `PROXY_IMAGE`, or `NTOPNG_IMAGE` for the live host). Floating tags are refused.
2. Read the upstream release notes first. Akvorado states that downgrade is not supported and that the orchestrator should be upgraded before the other Akvorado services, because it migrates ClickHouse. To follow that order, update the orchestrator service by hand, then run the deploy:

   ```bash
   docker service update --image <new akvorado image> sknet-<env>_orchestrator
   ```

3. Run the deploy again. Each Swarm config is named after its content, so a changed setting rolls only the services that mount it.
4. For ntopng, rerun `live.yml`; a changed image or unit restarts the container.

Roll back by restoring the old tag only if the new version did not migrate the database. Take a copy of `DATA_DIR/clickhouse` first for anything big.

## Remove

- **Exporters:** `sknet_state: absent`, run `exporters.yml`. Leaves the softflowd package.
- **Live view:** `sknet_state: absent`, run `live.yml`. Removes both units, the container, the script, the `SKNET-LIVE` chain and its `INPUT` hook. The image stays, and so does the data directory unless `sknet_live_purge_data: true`.
- **Stack:** `docker stack rm sknet-<env>` on a manager. Data stays in `DATA_DIR` until you delete it.
- **Flow port allowlist:** the deploy has no switch for removal. Include `tasks/port_allowlist.yml` with `sknet_fw_state: absent`, or by hand on `DATA_NODE`: `systemctl disable --now sknet-<env>-fw.service`, delete `/etc/systemd/system/sknet-<env>-fw.service` and `/usr/local/sbin/sknet-<env>-fw`, then remove the chain `SKNET-<ENV>` and its hooks in `INPUT` and `DOCKER-USER` (`iptables -D`, `-F`, `-X`).
- **Data:** delete `DATA_DIR` last, after you are sure you no longer need the history.

## Troubleshooting

### No flows in the console

Work from the exporter towards the console.

1. **Exporter running?** `systemctl status 'sknet-softflowd@*'`. A unit in a restart loop usually means a wrong interface name or index; see the journal.
2. **Packets leaving?** On the exporter, `tcpdump -ni any udp port 2055 and dst host <collector>`. softflowd ignores its own export traffic, so a quiet interface sends nothing.
3. **Packets arriving?** On the collector node, `tcpdump -ni any udp port 2055`. If they leave but do not arrive, a firewall or the mesh policy is in the way (for a one-way site, the telemetry exception).
4. **Allowlisted?** `sudo iptables -L SKNET-<ENV> -n -v`. The exporter's source address must be in `FLOW_SOURCES`; a rising DROP counter means it is not. Fix the vault and redeploy.
5. **Metadata matches?** `docker service logs sknet-<env>_outlet` showing "metadata missing" for an address means that address is not in the exporter metadata. Redeploy with the exporter inventory (`-i`) after adding the host.
6. **Is ClickHouse writing?** If the disk is near `MIN_FREE_GB`, writes stop. Check free space.
7. **Port mismatch.** The exporters send to the vault's `NETFLOW_PORT`; a per-host `sknet_netflow_port` that differs is refused. The inlet listens on exactly one port.

### Wrong exporter identity behind NAT

The collector identifies an exporter by the **source address of its packets**. If the exporter's traffic crosses a NAT or a masquerading router, the collector sees the translated address, not the host's own. Symptoms: the flows are dropped by the allowlist, or they arrive but Akvorado shows the wrong exporter name or no interface names.

Fix: set `sknet_export_address` to the address the collector actually sees (confirm with `tcpdump` on the collector), put the same address in `FLOW_SOURCES`, redeploy the stack and rerun `exporters.yml`. If two hosts share one translated address they cannot be told apart: give each its own address (route them over the mesh, not through the NAT).

### 403 on the console

A 403 comes from the allowlist, before sign-in.

- The client's address is not in `ALLOW_CIDRS`. Remember the allowlist judges the address Traefik sees. If Traefik sits behind NAT or a tunnel, every client looks like the tunnel's address; make Traefik see real addresses (host-mode entrypoints) or rely on SSO.
- A resolver outside the mesh returned the public address. That client is meant to be refused.
- If you get an Authentik sign-in loop or a console with no user instead, the forward-auth headers are not arriving (`X-authentik-username` is empty); check the SSO middleware on the router.

### 403 "forbidden" from the gate

A plain `forbidden` body (not the allowlist's 403) comes from the gate: the request did not carry the right `X-Sknet-Gate` header. Traefik adds it through the `sknet-<env>-gate` middleware, which both routers must end with. After changing `GATE_SECRET`, redeploy so the Traefik label and the gate's Docker secret change together. A request that reaches the gate any other way (another container on `cloud-public-<env>`) is refused by design.

### Live view returns 502 or times out

Work through these in order:

1. Where is the `gate` running (`docker service ps sknet-<env>_gate`)? On another swarm node its traffic reaches ntopng from that node's address, which must be in `sknet_live_allow_sources`. On the live host itself (when that is also a swarm node) the traffic arrives from the `docker_gwbridge` subnet, not the node address, and `SKNET-LIVE` drops it: adding the node address does not help. Keep the gate off the live host (the deploy does that by default when the live hosts are in the inventory; or set `PROXY_PLACEMENT_CONSTRAINTS`), or add the host's `docker_gwbridge` subnet to `sknet_live_allow_sources` (this admits every container on that host).
2. Is `LIVE_UPSTREAM` exactly `http://<sknet_live_bind>:3000`? The deploy warns when it matches no host of `sknet_live_hosts`.
3. Is ntopng up (`systemctl status sknet-live`) and bound to that address?
4. After a host firewall reload (ufw, firewalld) the `SKNET-LIVE` hook may be gone: `systemctl restart sknet-live-fw`.

### ntopng memory

ntopng is capped (`sknet_live_memory_max`, default 512M, applied to the container with swap included and to the unit). When it hits the cap the container is killed and restarted (`Restart=on-failure`, 10 seconds), which looks like the live view dropping every few hours on a busy link. Options, in order:

1. Lower `sknet_live_max_hosts` and `sknet_live_max_flows` (16384 each by default).
2. Capture fewer interfaces (`sknet_live_interfaces`).
3. Raise `sknet_live_memory_max` if the host can spare it. On a small gateway keep it low: the cap exists to protect the gateway.

Rerun `live.yml` after changing a hostvar.
