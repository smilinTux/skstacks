# SKAgent Expose — Tailscale Serve/Funnel

Expose the SKComm API (and Profile API) to your tailnet or the public internet.

## Prerequisites

- SKComm API running on `127.0.0.1:9384` (via `uvicorn skcomm.api:app --port 9384`)
- Tailscale installed and authenticated (`tailscale up`)
- HTTPS certificates enabled in Tailscale admin console

## Tailscale Serve (tailnet only)

Makes the API accessible at `https://<hostname>.<tailnet>.ts.net/` from any device on your tailnet.

### Manual

```bash
tailscale serve --bg --https=443 http://127.0.0.1:9384
```

### Systemd user service

```bash
# Install
mkdir -p ~/.config/systemd/user
cp skagent-expose.service ~/.config/systemd/user/

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now skagent-expose.service

# Check status
systemctl --user status skagent-expose.service
tailscale serve status
```

### Stop

```bash
systemctl --user stop skagent-expose.service
# or manually:
tailscale serve off
```

## Tailscale Funnel (public internet)

Exposes the API publicly at `https://<hostname>.<tailnet>.ts.net/`. Anyone with the URL can reach it (authentication still required via CapAuth).

```bash
tailscale funnel --bg --https=443 http://127.0.0.1:9384
```

To stop: `tailscale funnel off`

## Accessing the Profile API

From any device on the tailnet (or internet if using Funnel):

```bash
# Generate a token on the host machine
TOKEN=$(skcapstone token generate --ttl 86400)

# Access from any device
curl -H "Authorization: Bearer $TOKEN" \
  https://myhost.tail12345.ts.net/api/v1/profile

curl -H "Authorization: Bearer $TOKEN" \
  https://myhost.tail12345.ts.net/api/v1/profile/memories?limit=5

# Access the PWA
open https://myhost.tail12345.ts.net/app/
```

## Security

- All endpoints require CapAuth bearer token authentication
- Tailscale Serve uses WireGuard encryption (tailnet traffic)
- Tailscale Funnel adds TLS termination for public access
- Tokens are time-limited (default 24h via `--ttl 86400`)
