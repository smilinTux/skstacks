# exposure/pangolin — sovereign tunnel ingress (cloudflared replacement)

Pangolin is the **self-hosted** version of a Cloudflare Tunnel: expose internal
services publicly **without opening any WAN ports or holding a static IP**, but
keep the whole path sovereign. It's the same *role* as `cloudflared/` — just not
dependent on Cloudflare.

## Topology
```
  Internet ─► Pangolin + Gerbil (public edge node: dashboard + WireGuard server)
                   ▲  encrypted WireGuard tunnel (outbound from the cluster)
                   │
              Newt connector (runs IN the cluster) ──► Traefik Gateway :443
```
- **Pangolin + Gerbil** run on a small public-IP node (the only public surface).
- **Newt** runs next to your services and dials *out* to Gerbil — no inbound ports
  on the cluster. Newt forwards to the in-cluster Traefik Gateway, so all the
  HTTPRoutes/TLS you already have work unchanged.

## The two exposure ladders
| Plane | Ease (managed) | Sovereign target |
|---|---|---|
| **Mesh** (private device/team access) | Tailscale | **Netbird** |
| **Tunnel ingress** (public, no open ports) | cloudflared | **Pangolin** |

Same pattern both rows: start easy, graduate to sovereign. Pangolin and cloudflared
are interchangeable at the tunnel layer; pick Pangolin when you want no third-party
in the data path.

## License note
Pangolin is **dual-licensed AGPLv3 + Fossorial Commercial License** — free for
personal use and businesses under **$100K gross annual revenue**; EE features above
that need a commercial license. Sovereign-safe for single-op/small-team; record the
revenue threshold in your decision log.

## Files
- `swarm-newt.yml` / `k8s-newt.yaml` — the Newt connector (cluster side). `NEWT_ID`
  / `NEWT_SECRET` / `PANGOLIN_ENDPOINT` come from the secret backend.
- The public Pangolin+Gerbil node is provisioned separately (its own small stack on
  the edge VPS) — it is the only component that needs a public IP.

> Naming (from the skchat session): for tunnel-fronted FQDNs, flatten nested
> clusters to a single level (`sk*-skstack01.example.com`) so a one-level wildcard
> cert covers them.
