# skdns/blockchain — censorship-resistant naming (Handshake + ENS)

For names that must survive registrar/government takedown, skdns resolves
**decentralized roots** in addition to ICANN DNS:

- **Handshake (HNS)** — a blockchain root zone where you *own* the TLD outright
  (no registrar can revoke it). The sovereign-root play.
- **ENS (.eth)** — Ethereum Name Service; resolve `.eth` names to content/records.

These are the naming side of `skdweb` (IPFS + IPFS-Cluster + HNS/ENS) — see
`cloud/skdweb/`. skdns wires resolution so clients can reach them:

- Run an **HNS resolver** (hnsd / Technitium with the Handshake root hints) and
  point the recursive resolver's root at it for `.hns`/Handshake TLDs.
- Bridge **ENS** via an ENS-DoH gateway (or a local resolver plugin) for `.eth`.
- Keep ICANN resolution (Cloudflare/Technitium) for normal TLDs — the resolver
  routes by TLD: blockchain TLDs → decentralized roots, everything else → ICANN.

> Posture (from the stack-validation): **Handshake is the sovereign-root primary**;
> ENS is optional and only if you need Ethereum-address resolution. Adoption is
> still early, so run blockchain naming *alongside* ICANN, not as the sole path.
