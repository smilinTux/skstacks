# sknet architecture

This page describes what runs where, which ports are open to whom, and which parts you must keep private. Names are placeholders: `site-a`, `gateway-1`, `collector-1`, addresses from 192.0.2.0/24 and 198.51.100.0/24, domain `example.com`.

## Components

```mermaid
flowchart TB
  subgraph EXP["exporter hosts (host play exporters.yml)"]
    SF["sknet-softflowd@IFACE.service<br/>one per interface, NetFlow v9, unsampled"]
  end
  subgraph LIVE["live host (host play live.yml)"]
    NT["sknet-live.service<br/>ntopng container, host network, tcp 3000"]
    NFW["SKNET-LIVE iptables chain"]
  end
  subgraph SWARM["swarm stack sknet-ENV, every stateful service pinned to DATA_NODE"]
    FW["SKNET-ENV iptables chain<br/>sknet-ENV-fw.service"]
    IN["inlet<br/>udp 2055 + 4739, host mode"]
    KF["kafka (KRaft, 1 node)"]
    OU["outlet"]
    CH[("clickhouse<br/>local disk")]
    OR["orchestrator"]
    CO["console :8080<br/>sknet-ENV network only"]
    GT["gate (caddy) :8080<br/>the only service on cloud-public<br/>needs the X-Sknet-Gate header"]
  end
  TR["Traefik<br/>middleware sknet-ENV-allow + SSO"]
  SF -- "udp NETFLOW_PORT" --> FW --> IN --> KF --> OU --> CH
  OR -. "config, topic, schema" .-> IN & OU & CH
  CO --> CH
  TR -- "adds X-Sknet-Gate" --> GT --> CO
  GT -- "live host only, http, IPv4 literal" --> NFW --> NT
```

| Piece | Where it runs | Role |
|---|---|---|
| `inlet` | swarm, on `DATA_NODE` | Listens for NetFlow v9 and IPFIX, wraps datagrams and writes them to Kafka. Parses nothing. |
| `kafka` | swarm, on `DATA_NODE` | Single-node KRaft broker used only as a buffer. Retention is 12 hours and a size cap taken from `DISK_BUDGET_GB`. |
| `outlet` | swarm, on `DATA_NODE` | Reads Kafka, decodes, adds interface names and classes, inserts into ClickHouse. |
| `clickhouse` | swarm, on `DATA_NODE` | Flow storage. Low-memory profile, system logs trimmed, stops writing when free space drops below `MIN_FREE_GB`. |
| `orchestrator` | swarm, on `DATA_NODE` | Creates the Kafka topic, builds the ClickHouse schema and TTLs, serves configuration to the others. |
| `console` | swarm, on `DATA_NODE` | The Akvorado web UI. On the private `sknet-<env>` network only; reached through the gate. |
| `gate` | swarm, any node except the live host (see `PROXY_PLACEMENT_CONSTRAINTS`) | Caddy, always deployed, the only sknet service on the shared `cloud-public-<env>` network. Both Traefik routers (`sknet[-env].<cluster>.<domain>` and, with `LIVE_UPSTREAM`, the live host) point at it. It requires the secret `X-Sknet-Gate` header, then proxies the console host to the console and the live host to the ntopng host. |
| `softflowd` | exporter hosts | One systemd instance per interface: `sknet-softflowd@<interface>.service`. |
| `ntopng` | live host | One container, host networking, memory-capped, web UI on one private address, port 3000. |

Upstream services that sknet leaves out: Akvorado's bundled Traefik, Valkey, GeoIP updater, Kafka UI, Grafana, Prometheus and demo exporters. The upstream compose file cannot be deployed to swarm as it is (it uses `extends`), so sknet ships one flattened stack file.

## Ports

| Port | Proto | Where | Open to |
|---|---|---|---|
| `NETFLOW_PORT` (2055) | udp | `DATA_NODE`, host mode | only addresses in `FLOW_SOURCES` |
| `IPFIX_PORT` (4739) | udp | `DATA_NODE`, host mode | only addresses in `FLOW_SOURCES` |
| 8080 | tcp | gate container (the console listens on it too, on `sknet-<env>` only) | the swarm overlay only: Traefik to the gate, the gate to the console |
| 3000 | tcp | live host, on `sknet_live_bind` only | only `sknet_live_allow_sources` |
| 9092, 9000, 8123 | tcp | Kafka, ClickHouse | the `sknet-<env>` overlay only, never published |

The inlet's UDP ports are published in host mode so the exporter's source address reaches the inlet unchanged. That address is the exporter's identity in Akvorado. The swarm routing mesh would rewrite it, which is why it is not used.

### The flow port allowlist

Published ports bypass most host firewall rules, so the deploy installs its own. A oneshot systemd unit `sknet-<env>-fw.service` runs `/usr/local/sbin/sknet-<env>-fw`, which fills an iptables chain `SKNET-<ENV>` and hooks it into `INPUT` and `DOCKER-USER`. For each flow port it returns for every address in `FLOW_SOURCES` and drops everything else. The match uses the original destination port, so it also holds after Docker's address translation. The unit is re-applied after a reboot or a Docker restart. It is ordered after `docker.service`, so for a few seconds after a reboot (until the unit has run) the host-mode ports accept datagrams from any address; the inlet only accepts NetFlow into Kafka, so the exposure is small. `FLOW_SOURCES` takes IPv4 literals only (not names, not CIDRs, not IPv6), and an empty list refuses the deploy.

## Web access

Every sknet router carries the middleware chain `sknet-<env>-allow`, then `TRAEFIK_MIDDLEWARES`, then (in `authentik` mode) `authentik@file`. The allowlist comes first, so an address outside `ALLOW_CIDRS` is refused before it ever reaches the sign-in provider, and gets a plain 403.

`ALLOW_CIDRS` defaults to the three RFC 1918 private ranges; add your overlay mesh range if it uses shared address space (RFC 6598). Narrow it to your LAN and mesh ranges.

### `AUTH_MODE`

| Mode | Gate | Console user |
|---|---|---|
| `authentik` (default) | `ALLOW_CIDRS` then Authentik forward auth | the login from the `X-authentik-username`, `X-authentik-name` and `X-authentik-email` headers; no header means no access |
| `allowlist` | `ALLOW_CIDRS` only | one fixed user (`sknet-allowlist`) for everyone the allowlist admits |

Rules the deploy enforces:

- Any other `AUTH_MODE` is refused.
- `SSO_MIDDLEWARE` is valid only with `AUTH_MODE: authentik`.
- **`allowlist` is refused when `CLOUDFLARED` is true.** Behind a tunnel every request arrives from the tunnel, the allowlist cannot tell clients apart, and with no SSO everyone would be admitted as the fixed user.

### Which address the allowlist judges

The allowlist uses the direct remote address Traefik sees (`ipStrategy` depth 0), and trusts no `X-Forwarded-For`. That is the real client only when Traefik's entrypoints see client addresses: host-mode ports with no NAT, proxy or tunnel in front. Behind routing-mesh NAT every client looks private and passes `ALLOW_CIDRS`. If you cannot give Traefik real client addresses, use `authentik` mode and treat SSO as the real gate.

### Header trust and the gate

The console and Akvorado trust the login header Authentik's forward auth sets (`X-authentik-username`, `-name`, `-email`), and ntopng has no login of its own. Anything that can reach them directly, skipping Traefik, would be anyone, with no allowlist and no sign-in.

**`cloud-public-<env>` is the shared boundary.** It is the overlay every routed service of every stack sits on, so any container on it can be hostile or compromised. The stack therefore puts only one service there, `gate` (Caddy), and the console moves to the private `sknet-<env>` network:

- Traefik adds a request header, `X-Sknet-Gate`, to both routers after the allowlist and the sign-in. Its value is the vault key `GATE_SECRET` (required, at least 32 characters; `openssl rand -hex 32`).
- Each host block of the gate's Caddyfile answers 403 to any request that does not carry exactly that value, and strips the header before proxying. The secret is read from a Docker secret file, never from the Caddyfile. A container that reaches the gate over `cloud-public-<env>` without it gets a 403, so it cannot forge a login header or reach ntopng.
- The console host proxies to `sknet-<env>_console:8080` (the stack-qualified name) over `sknet-<env>`, passing the Authentik identity headers through. `/debug` (pprof and internals) is answered with 404 on that host. The live host proxies to `LIVE_UPSTREAM`.

What this does not cover: the secret appears in the Traefik label of the gate service, so anyone who can read Swarm service specs (a manager) can read it, and a container that can read Traefik's own configuration can too. Treat `GATE_SECRET` as a deploy-time secret, rotate it by changing the vault key and redeploying, and keep it out of logs. Keep `sknet-<env>` private as a second line: attach nothing else to it, because anything there can reach the console without the gate.

The deploy cannot check what else you attach to either network.

## The live view

ntopng runs as `sknet-live.service`: `docker run` with host networking, `--memory` and `--memory-swap` set to `sknet_live_memory_max` (default 512M, also `MemoryMax` on the unit), all capabilities dropped except the few capture needs, and `no-new-privileges`. It captures only the interfaces in `sknet_live_interfaces` (1 to 8) and listens on `sknet_live_bind:3000`.

ntopng's own login is disabled (`-l 1`, everyone is admin) because the proxy provides sign-in. That is why the host play refuses a wildcard bind (`0.0.0.0`), refuses a public bind address unless `sknet_live_allow_public_bind: true`, and refuses an empty `sknet_live_allow_sources`. The chain `SKNET-LIVE` (hooked into `INPUT` for tcp 3000) accepts only that list and drops the rest.

**Gate source address.** Where the `gate` runs decides which address ntopng sees:

- On another swarm node, its traffic leaves from that node's address (masqueraded out of `docker_gwbridge`). `sknet_live_allow_sources` must list the address of every swarm node that can run the gate.
- On the live host itself, when that host is also a swarm node, the packet is delivered locally and never masqueraded. It reaches `INPUT` from the `docker_gwbridge` subnet with the container's own address, which the node address in the list does not cover, so `SKNET-LIVE` drops it.

So the gate is kept off the live host by default. When the deploy runs with the live hosts in the inventory (`sknet_live_hosts`), it renders `node.hostname != <inventory name>` for each of them (the inventory name must be the swarm node hostname). `PROXY_PLACEMENT_CONSTRAINTS` replaces that default. The alternative on a live host that is also a swarm node is to add its `docker_gwbridge` subnet to `sknet_live_allow_sources`, which admits every container on that host, not just the gate. `live.yml` prints a warning when the host is an active swarm node. A gate on an unlisted address shows up as a 502 or a timeout on the live hostname.

**Other services on tcp 3000.** The `SKNET-LIVE` hook matches only `sknet_live_bind`, so another host-network service on port 3000 on the same machine is not firewalled by it. After a host firewall reload (ufw, firewalld) that flushes `INPUT`, rerun `systemctl restart sknet-live-fw` to put the hook back.

**Redis.** The ntopng container bundles a Redis on the host's loopback port 6379. A host Redis already on that port makes the two clash and would silently receive ntopng's preferences.

`LIVE_UPSTREAM` is `http://<IPv4 literal>:<port>`. Names are refused so the target cannot change under the proxy.

## Data and retention

| Setting | Effect |
|---|---|
| `DATA_NODE` | Required. The swarm node that holds Kafka and ClickHouse. The deploy refuses to run without it. |
| `DATA_DIR` | Local disk on that node, default `/var/lib/sknet-<env>`. Never under `/var/data` (shared storage) and never NFS. |
| `FLOW_TTL_DAYS` (30) | Raw table and the 1 minute table. |
| `FLOW_AGG_TTL_DAYS` (90) | The 5 minute table; the hourly table keeps 4 times as long (360 days). At most 365. |
| `DISK_BUDGET_GB` (20) | Planning figure. The deploy refuses a disk where what `DATA_DIR` already uses plus what is free is below it. Also sets Kafka's size cap (one twentieth of the budget per partition). |
| `MIN_FREE_GB` (5) | Hard stop. ClickHouse stops writing when the disk has less free than this. |

Decide `FLOW_TTL_DAYS` before the first start: ClickHouse sizes partitions from the TTL when it creates the tables, and changing it by a large factor later does not repartition them.

## Exporter identity and metadata

Akvorado has no SNMP here. The outlet takes interface names, speeds and classes from static metadata keyed by the exporter's source address. The deploy builds it from the hostvars of the inventory group `sknet_exporter_hosts` (run the deploy with `-i <exporters ini>`), or, if that group is absent, from the vault map `STATIC_EXPORTERS`. With neither it refuses to deploy rather than render a collector that discards every flow.

Each interface has a name, a forced interface index of 100 or more (unique per host) and a class:

| Class | Meaning | Boundary in Akvorado |
|---|---|---|
| `lan` | a local network | internal |
| `mesh` | an overlay mesh interface | internal |
| `estate-link` | a tunnel to another estate | internal |
| `internet` | a public-facing interface | external |

softflowd reports the same interface index as both input and output and gives no direction, so group by exporter and interface, not by direction. See the [multi-hop pattern](multi-hop-pattern.md) for what that means for traffic that crosses two monitored interfaces.

## Trust boundaries

```mermaid
flowchart LR
  subgraph PRIV["private, never published"]
    KF["kafka"] --- CH[("clickhouse")] --- OU["outlet"] --- OR["orchestrator"]
  end
  subgraph GATED["gated by an address allowlist"]
    FL["flow ports udp 2055 / 4739<br/>FLOW_SOURCES"]
    WEB["web routers<br/>ALLOW_CIDRS + SSO"]
    LV["ntopng tcp 3000<br/>sknet_live_allow_sources"]
  end
  NET(["anything else on the network"]) -. "dropped / 403" .-x GATED
  NET -. "no route" .-x PRIV
```

Nothing in sknet is meant to face the internet. There is no public router and no public flow port.
