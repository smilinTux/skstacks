# SKStacks — Sovereign Infrastructure Configs

Infrastructure-as-code configurations for the SKWorld sovereign stack.
Each subdirectory is a self-contained service deployment.

---

## Services

| Directory | Service | Purpose |
|-----------|---------|---------|
| `coturn/` | [coturn](https://github.com/coturn/coturn) TURN/STUN server | WebRTC NAT traversal relay for `turn.skworld.io` |

---

## coturn — Sovereign TURN Server

STUN/TURN server for WebRTC ICE negotiation. Used by SKComm's WebRTC transport
and SKChat voice/data channels as the NAT traversal fallback when direct P2P fails.

**Domain**: `turn.skworld.io`

### Architecture

```
WebRTC Peer A                coturn                WebRTC Peer B
     │                          │                         │
     │── STUN binding req ──────→                         │
     │←── mapped address ────────                         │
     │                          │                         │
     │  (direct ICE fails)       │                         │
     │── TURN Allocate ──────────→                         │
     │←── relay address ─────────                         │
     │── TURN CreatePermission ──→ (for Peer B's IP)       │
     │                          │                         │
     │══ encrypted DTLS-SRTP through relay ════════════════│
```

For Tailscale-connected peers, the Tailscale 100.x IP is used as a host ICE candidate
and Tailscale DERP handles relay — coturn is only needed for non-tailnet peers.

### Quick Setup

**1. Install coturn:**
```bash
# Debian/Ubuntu
sudo apt install coturn

# Arch/Manjaro
sudo pacman -S coturn
```

**2. Generate a shared secret:**
```bash
openssl rand -hex 32
# → copy this into turnserver.conf static-auth-secret AND
#   into ~/.skcomm/config.yml transports.webrtc.settings.turn_secret
#   (or set SKCOMM_TURN_SECRET env var)
```

**3. Deploy the config:**
```bash
sudo cp coturn/turnserver.conf /etc/coturn/turnserver.conf
# Edit: set static-auth-secret, uncomment external-ip=YOUR_PUBLIC_IP
```

**4. Set up TLS (recommended):**
```bash
certbot certonly --standalone -d turn.skworld.io
# Then uncomment the cert/pkey lines in turnserver.conf
```

**5. Open firewall ports:**
```bash
ufw allow 3478/udp    # STUN/TURN
ufw allow 3478/tcp    # TURN over TCP
ufw allow 5349/tcp    # TURN over TLS
ufw allow 49152:65535/udp  # UDP relay range
```

**6. Start and enable:**
```bash
sudo systemctl enable --now coturn
sudo systemctl status coturn
```

### Configuration Summary

| Setting | Value |
|---------|-------|
| STUN/TURN port | 3478 (UDP + TCP) |
| TLS TURN port | 5349 |
| UDP relay range | 49152–65535 |
| Realm | `turn.skworld.io` |
| Auth | HMAC-SHA1 time-limited credentials |
| Credential TTL | 86400s (24h) |
| Private IP relay | Denied (SSRF protection) |

### Client Integration (SKComm)

```yaml
# ~/.skcomm/config.yml
transports:
  webrtc:
    enabled: true
    priority: 1
    settings:
      turn_server: "turn:turn.skworld.io:3478"
      turn_secret: "${SKCOMM_TURN_SECRET}"   # same secret as static-auth-secret
```

The SKComm WebRTC transport generates HMAC-SHA1 time-limited credentials automatically:
```python
# credential = HMAC-SHA1(secret, f"{expiry}:{username}")
# username    = f"{expiry}:{agent_name}"
```

### Testing

```bash
# Test STUN binding (should return your public IP)
stun turn.skworld.io

# Test TURN allocation (requires valid credentials)
turnutils_uclient -u "<expiry>:<username>" -w "<credential>" turn.skworld.io

# Check logs
journalctl -u coturn -f
tail -f /var/log/coturn/turnserver.log
```

---

## License

GPL-3.0-or-later — Part of the [smilinTux](https://github.com/smilinTux) sovereign stack.
