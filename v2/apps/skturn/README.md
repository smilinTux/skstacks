# skturn — standalone coturn (WebRTC TURN/STUN)

The **media-relay plane** for WebRTC. skchat/skvoice/LiveKit handle signaling over
the HTTP/WS plane (→ Traefik :443 via the exposure tiers); the actual A/V media is
UDP and **cannot** traverse Traefik or cloudflared. coturn is therefore host-published
on a public-reachable edge node, DNS **grey-cloud** (DNS-only, never proxied).

Decoupled from skhub so TURN no longer dies when the skhub stack is down.

## The shared secret (read first)
The TURN static-auth-secret is the **EXISTING `skhub.turn_secret`** — already shared
by **Nextcloud Talk (spreed)**, **netbird**, and **skchat**.
- **DO NOT generate a new secret** — it would break all three consumers.
- Migrate the existing value into a docker secret (never commit it):
  ```bash
  # value pulled from the secret backend, NOT typed/echoed into history
  printf '%s' "$(skvault get skhub turn_secret)" | docker secret create coturn_static_auth_secret -
  ```
- **Never** reuse the leaked Nextcloud app-password (`NC_PASS`) for anything.

## DNS + ports
- `turn-<cluster>.<domain>` (e.g. `turn-skstack01.example.com`) **A → edge public IP**, **grey-cloud**.
- Open on the edge node (UFW) + publish in the engine:
  `3478/udp`, `3478/tcp`, `5349/tcp`, `5349/udp` (turns), and the relay range
  **`49160-49200/udp`**.
- Label the edge node: `docker node update --label-add edge=true <node>`.

## Deploy
```bash
docker stack deploy -c apps/skturn/docker-compose.yml skturn
# open the relay range (compose long-syntax lists one port; open the whole range):
sudo ufw allow 49160:49200/udp
sudo ufw allow 3478,5349/udp ; sudo ufw allow 3478,5349/tcp
```
turns TLS cert/key live in the `skturn-certs` volume (ACME **DNS-01**, since 80/443
aren't free on the edge node).

## Repoint the 3 consumers (secret UNCHANGED — only the URL changes)
1. **Nextcloud Talk:**
   `occ config:app:set spreed turn_servers --value='[{"server":"turn-<cluster>.<domain>:3478","secret":"<existing>","protocols":"udp,tcp"}]'`
2. **netbird** management: set `TURNConfig.Turns[].URI = turns:turn-<cluster>.<domain>:5349` (same secret).
3. **skchat:** `SKCHAT_TURN_URLS=turns:turn-<cluster>.<domain>:5349` — *owned by the skchat session* (connectivity.py tier-3).

> The prod repoint touches live Nextcloud/netbird config and migrates the shared
> secret — run it when the cluster is reachable (skhub is month-down). This repo
> ships the stack + runbook; the live migration is a gated operator step.

## Kubernetes variant
coturn on K8s = a `hostNetwork: true` DaemonSet pinned to edge nodes
(`nodeSelector: edge=true`), same args, secret via a `Secret` (ExternalSecret →
OpenBao). hostNetwork is required so the relay sees the real public IP.

## Naming note (from the skchat session)
For **cloudflared-fronted** FQDNs, flatten nested clusters to a single level
(`sk*-skstack01.example.com`, not `sk*.skstack01.example.com`) — the Cloudflare wildcard
cert only covers one level. coturn is grey-cloud so it's exempt, but this applies
to all Traefik-fronted services behind cloudflared.
