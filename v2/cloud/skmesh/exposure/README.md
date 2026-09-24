# skmesh / exposure — pluggable ingress entry points

**How external traffic reaches the cluster.** These adapters sit *on top of*
whatever you've already deployed — they all funnel to the **same in-cluster
Traefik Gateway** (`skstacks` Gateway, `traefik` namespace, :443). They are
**stackable**: you can run any combination at once (e.g. LAN + cloudflared +
Tailscale), and each is independently toggled per environment.

```
            ┌─────────── exposure adapters (stackable, optional) ───────────┐
  Internet ─┤ cloudflared tunnel (outbound, no open ports)                   │
  Tailnet  ─┤ tailscale (operator / serve)                                   │┐
  Mesh     ─┤ netbird (routing peer)                                         ││
  LAN      ─┤ lan (MetalLB VIP — the baseline)                               ││
            └───────────────────────────────────────────────────────────────┘│
                                   │ all route to                              │
                                   ▼                                           │
                        Traefik Gateway (:443)  ──► HTTPRoutes ──► services ◄──┘
```

| Adapter | Entry point | Inbound ports? | Sovereign? | Use when |
|---|---|---|---|---|
| [`lan/`](lan/) | MetalLB VIP on the LAN | yes (LAN only) | ✅ fully | Home/lab/on-prem — the default baseline |
| [`tailscale/`](tailscale/) | Tailnet (WireGuard) | no | ⚠️ hosted control plane (or Headscale) | Private remote access across your devices |
| [`cloudflared/`](cloudflared/) | Cloudflare Tunnel | **no** (outbound only) | ❌ Cloudflare-dependent | Public exposure without opening ports / static IP (easy) |
| [`pangolin/`](pangolin/) | Pangolin tunnel (self-hosted) | **no** (outbound only) | ✅ self-hostable (AGPL/$100K) | **Sovereign** public exposure — the self-hosted cloudflared |
| [`netbird/`](netbird/) | Netbird mesh (WireGuard) | no | ✅ self-hostable | Sovereign zero-trust remote access |

## Two ladders: start easy → graduate sovereign
| Plane | Ease (managed) | Sovereign target |
|---|---|---|
| **Mesh** (private device/team access) | Tailscale | **Netbird** |
| **Tunnel ingress** (public, no open ports) | cloudflared | **Pangolin** |

Same pattern both rows. Keep `lan/` always-on underneath as the bootstrap/recovery
baseline (it needs no external control plane). Headscale is supported but **not
recommended** (pre-1.0, hobbyist, Tailscale-client-coupled).

## The Netbird (and Tailscale/cloudflared) bootstrap caveat
You flagged it: a mesh/tunnel ingress has a **soft catch-22** — the adapter's
agent must reach its *control plane* (Netbird management, Tailscale coordination,
Cloudflare edge) to come up, and that reachability rides on something more
primitive. The escape hatch is the **`lan/` baseline**: MetalLB on the LAN needs
no external control plane, so it is always available to bootstrap/recover even if
every overlay is down. Recommended posture: **always keep `lan/` enabled**, and
layer the overlays on top. For Netbird specifically, host the management server
where the LAN baseline can reach it, so the mesh self-bootstraps.

## Two planes: HTTP/WS vs media
These exposure adapters cover the **HTTP/WS reachability plane** (webui, LiveKit
`wss` signaling) — all funnel to Traefik :443. WebRTC **media** (UDP) is a separate
plane: it cannot traverse Traefik or cloudflared (HTTP/TCP only). That's handled by
**`apps/skturn/`** (coturn) — host-published on a public edge node, DNS **grey-cloud**
(DNS-only, not proxied). skchat/skvoice consume the resulting `turn-<cluster>` FQDN.

## Toggling
Each adapter ships a k8s manifest and a Swarm compose. Enable an adapter by adding
it to the platform overlay's kustomization (k8s) or deploying its stack (Swarm).
Secrets (tunnel tokens, auth/setup keys) resolve from the secret backend — never
inline.
