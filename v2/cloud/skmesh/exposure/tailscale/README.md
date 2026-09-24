# exposure/tailscale — tailnet ingress (Tailscale / Headscale)

Expose the Traefik Gateway on a WireGuard tailnet. Two control-plane choices:

## The mesh ladder (RATIFIED)
**Tailscale for ease (including bootstrapping) → Netbird as the sovereign target.**
Tailscale is the lowest-friction way to get remote access immediately — use it
*during bootstrap* and for convenience. Then **graduate to Netbird**
(`exposure/netbird/`) as the recommended sovereign end state (own
clients/control/relay/policy/SSO, AGPL, funded, no Tailscale-client dependency).

| Control plane | Maturity (2026) | Recommendation |
|---|---|---|
| **Tailscale** (SaaS coordination) | Stable, polished | ✅ **Recommended for ease + bootstrap** (accept a hosted coordination server) |
| **Netbird** (`exposure/netbird/`) | Stable, funded, AGPL | ✅ **Recommended sovereign target** |
| **Headscale** (self-host coordination) | **Pre-1.0** (`v0.28.x`), hobbyist, **~6-monthly breaking changes**, Tailscale-client-coupled | ⚠️ **Supported, NOT recommended** — only if you must self-host *and* keep stock Tailscale clients |

## The control-plane catch-22 — and how to avoid it
A mesh/tailnet **cannot be the only path to the infra it runs on**. If Headscale
(or Netbird management, or Tailscale auth) is down or not-yet-deployed, you must
still be able to reach the box. Resolution = **bootstrap ordering**:

```
1. Reach the node over the `lan/` baseline (local IP / MetalLB — no external
   control plane, ALWAYS available).
2. Over LAN, deploy the control plane (Headscale / Netbird mgmt) onto a node the
   LAN can reach.
3. The tailnet/mesh now comes up → use it as the remote-access + deploy path.
```

**Rule:** host the coordination/management server where the `lan/` baseline can
reach it, so the mesh **self-bootstraps** and never depends on itself. Keep `lan/`
enabled permanently as the recovery path (same principle as the OpenBao no-catch-22
secret bootstrap — there is always a server-less/LAN root that depends on nothing).

## Files
- `k8s-tailscale.yaml` — Tailscale operator annotations on the Traefik Service.
- `swarm-tailscale.yml` — a `tailscale serve` node proxying the Gateway onto the tailnet.
Auth keys come from the secret backend; for a sovereign tailnet point `TS_*` /
`--login-server` at your Headscale URL.
