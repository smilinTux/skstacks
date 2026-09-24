# exposure/lan — MetalLB VIP (baseline)

The default, fully-sovereign entry point: the Traefik Gateway Service is type
`LoadBalancer`, and **MetalLB** assigns it a VIP from the pool in
`platform/kubernetes/base/metallb-config.yaml` (per-env in the overlays). Traffic
on the LAN hits the VIP → Traefik :443 → HTTPRoutes.

- No external control plane, no open WAN ports — **always available**, so it is the
  bootstrap/recovery path for every overlay exposure (see ../README.md catch-22).
- On Swarm: Traefik publishes :80/:443 on the host; the VIP is provided by
  Keepalived (`platform/swarm/ansible/roles/swarm-ha/templates/keepalived.conf.j2`).

Nothing extra to deploy — it's configured by the platform base. The overlay
adapters (tailscale/cloudflared/netbird) layer on top of this.
