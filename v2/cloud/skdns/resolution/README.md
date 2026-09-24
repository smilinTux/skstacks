# skdns / resolution — secure DNS options

DNS is the one dependency that can quietly take an entire stack down (or be used to
censor it). skdns supports **layered, swappable DNS** so you can be as sovereign,
secure, and resilient as a deployment needs.

Three planes + a transport property + failover:

| Plane | Option | What | Sovereign? |
|---|---|---|---|
| **Authoritative** (own your zones) | [`technitium/`](technitium/) | Self-host: authoritative **+** recursive **+** filtering **+** DoH/DoT/DoQ in one binary | ✅ fully |
| | PowerDNS + AdGuard Home | Split self-host (heavier) | ✅ fully |
| | Cloudflare (managed) | `infra/tofu/modules/cloudflare-dns` — **default for public zones** | ❌ hosted |
| **Naming roots** (censorship-resistant) | [`blockchain/`](blockchain/) | **Handshake (HNS)** TLD ownership + **ENS** (.eth) — can't be taken down by a registrar | ✅ decentralized |
| **Resolver** (what clients query) | Technitium / AdGuard | Filtering recursive resolver with blocklists | ✅ |
| **Transport** | secure-by-default | **DoH / DoT / DoQ** everywhere (no plaintext :53 over the WAN) | — |
| **Resilience** | [`failover/`](failover/) | Multi-provider authoritative, health-checked, auto-failover | — |

## Defaults (and the Tailscale exception)
- **Public zones → Cloudflare managed** by default (fast, anycast, free tier) via the
  cloudflare-dns tofu module. **Not required if you use Tailscale/Headscale** —
  MagicDNS resolves tailnet names internally, so a public authoritative provider is
  optional for tailnet-only services.
- **Internal/sovereign zones → Technitium** (self-host, all-in-one) — also the
  recommended low-footprint consolidation of the PowerDNS+AdGuard split.
- **Censorship-resistant naming → Handshake/ENS** (blockchain) for names that must
  survive registrar/government takedown.

## Secure transport
All resolvers serve **DoH (443/853), DoT (853), DoQ (853/udp)** and refuse plaintext
:53 from the WAN. Clients (incl. the cluster's own pods via the resolver) use
encrypted transport so queries can't be observed or tampered with in transit.

## Failover
`failover/` defines a primary + secondary authoritative provider with a health check
and an automatic cutover policy (e.g. Cloudflare primary → self-host/deSEC secondary,
or two self-host nodes in different locations). DNS resilience = no single provider
or zone can be a single point of takedown.
