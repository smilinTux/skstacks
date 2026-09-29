# skfetch: Sonarr, Radarr, Lidarr, Prowlarr and qBittorrent behind a VPN

Finds and downloads TV, movies and music into a media dataset that Plex
(`skstream`) can read. Everything that touches peers goes through a VPN
tunnel with a kill switch.

**Legal note:** a VPN hides traffic from your ISP; it does not make
downloading or seeding copyrighted material legal. Use it for content you
have the right to fetch.

## Architecture

A host-level docker compose project `skfetch-<env>` on the media node
`skfetch.MEDIA_NODE`, started by `skfetch[-<env>].service`:

| Service | Role | Network |
|---|---|---|
| gluetun | VPN client, kill switch, port forwarding | bridge `.2` |
| qbittorrent | torrent client | `network_mode: service:gluetun` (never published) |
| prowlarr | indexer manager | bridge `.10`, LAN port 9696 |
| sonarr / radarr / lidarr | TV / movies / music | bridge `.11` / `.12` / `.13`, LAN ports 8989 / 7878 / 8686, plus the skstream overlay to reach `plex` |
| flaresolverr (optional) | Cloudflare challenge solver for some indexers | bridge `.20` |

### Why host-level, not a swarm stack

qBittorrent must share gluetun's network namespace so it can only ever talk
through the tunnel (`network_mode: service:gluetun`); swarm has no such
mode. gluetun also needs `NET_ADMIN` and `/dev/net/tun`, which a swarm
service on Docker 27 cannot get. The unit is hardened like
`skstream-dumb.service`: it waits for swarm before the *arr apps join the
skstream overlay (`sk-wait-swarm-net`), is `WantedBy=docker.service`, and
has `StartLimitIntervalSec=0`.

### Storage

- `MEDIA_PATH` (default `/var/data/skfetch-<env>`) is mounted at `/data`:
  `downloads/`, `incomplete/` and `media/{tv,movies,music}`. Incomplete
  downloads stay on this dataset (not local disk) so a large torrent cannot
  fill the node's root disk, and imports are same-filesystem moves.
  Make it its own ZFS dataset with a quota (for example 1T) and exclude it
  from backups: it is re-fetchable and would dominate every snapshot.
- *arr, Prowlarr and qBittorrent config (SQLite) is local disk under
  `LOCAL_ROOT` (default `/var/lib/skfetch`, or `/var/lib/skfetch-<env>`).
  SQLite on NFS locks up. Back this up with the node.

### Seeds (only if absent)

`qBittorrent.conf` and each `config.xml` are written on first install only
(`FORCE_SEED`, default false, rewrites them):

- qBittorrent: bound to `tun0`, anonymous mode, save to `/data/downloads`,
  incomplete in `/data/incomplete`, speed cap and max-active knobs, stop
  seeding at ratio `QBT_MAX_RATIO` (1) or `QBT_MAX_SEEDING_MINUTES` (60),
  WebUI auth skipped only for the bridge subnet.
- *arr `config.xml`: `ApiKey` from the vault, `AuthenticationMethod`
  External with `AuthenticationRequired` Enabled (the reverse proxy does
  auth), and `AllowedHosts` = LAN address, node hostname, localhost,
  127.0.0.1, the public FQDN and the bridge IP. Current *arr versions answer
  `400 Bad Request` to a Host header missing from AllowedHosts, so the public
  FQDN must be there.

### Post-deploy wiring (idempotent)

`wire.py` (stdlib only) runs on the media node after the unit starts and
creates whatever is missing, matched by name or path:

- root folders `/data/media/tv`, `/data/media/movies`, `/data/media/music`
- qBittorrent download client (gluetun bridge IP, port 8080, categories
  `tv` / `movies` / `music`, remove completed downloads)
- Plex notification (host `plex:32400`, token from the vault, path mapping
  `/data/media/` to `PLEX_MEDIA_MOUNT`/). Lidarr refuses it until Plex has a
  Music library; that is a warning, rerun after adding the library.
- Prowlarr applications for Sonarr, Radarr and Lidarr (full sync)
- FlareSolverr indexer proxy with tag `flaresolverr`
- public indexers `INDEXERS`, and `INDEXERS_FLARESOLVERR` tagged
  `flaresolverr` (Cloudflare-fronted ones)

## Security notes

- **Kill switch:** gluetun blocks all egress when the tunnel is down. Verify
  it after a deploy by stopping openvpn inside gluetun and checking that
  qBittorrent has no egress; it must fail, not fall back to the host IP.
- **Never expose qBittorrent.** It has no published port and is not routed
  by Traefik; its WebUI trusts the bridge subnet without a password.
- With External auth the *arr UIs have no login of their own. Bind them only
  to a LAN address you trust (`LAN_BIND_ADDRESS`, default `127.0.0.1`) and
  put Traefik with forward auth in front for anything else.
- **Authentik:** give EACH host (sonarr, radarr, lidarr, prowlarr) its own
  forward_single provider and application bound to an admins group. A
  domain-level catch-all provider admits every Authentik user.
- On NFS the Traefik file watcher does not see a new dynamic file written
  from another node; `touch` it on each manager (or restart Traefik) after
  the first deploy.
- Plex token: a dedicated account token, never the server
  `PlexOnlineToken` (see `optional/skstream/README.md`).

## Vault keys

Namespace `skfetch`. Required: `MEDIA_NODE`, `OPENVPN_USER`,
`OPENVPN_PASSWORD`, `SONARR_API_KEY`, `RADARR_API_KEY`, `LIDARR_API_KEY`,
`PROWLARR_API_KEY`.

| Key | Default | Notes |
|---|---|---|
| `CLUSTERNAME`, `DOMAIN` | (required) | public hosts `<app>[-<env>].<CLUSTERNAME>.<DOMAIN>` |
| `MEDIA_NODE` | (required) | inventory host = docker node that runs the project |
| `OPENVPN_USER`, `OPENVPN_PASSWORD` | (required) | VPN account, only in `skfetch.env` (0600) |
| `WIREGUARD_PRIVATE_KEY` | `''` | only with `VPN_TYPE: wireguard` |
| `VPN_SERVICE_PROVIDER` | `private internet access` | any gluetun provider |
| `VPN_TYPE` | `openvpn` | |
| `SERVER_REGIONS` | `''` | pick a region that supports port forwarding |
| `VPN_PORT_FORWARDING` | `on` | forwarded port is pushed to qBittorrent |
| `SONARR_API_KEY`, `RADARR_API_KEY`, `LIDARR_API_KEY`, `PROWLARR_API_KEY` | (required) | 32 hex chars each, e.g. `openssl rand -hex 16` |
| `PLEX_TOKEN` | `''` | dedicated account token (plex.tv/link PIN flow); empty skips the Plex notification |
| `PLEX_TOKEN_IS_SERVER_TOKEN` | `false` | guard: `true` stops the play |
| `PLEX_HOST` | `plex` | Plex service name on the stream overlay |
| `PLEX_MEDIA_MOUNT` | `/skfetch/media` | where Plex sees `MEDIA_PATH/media` (skstream `PLEX_EXTRA_BINDS`) |
| `MEDIA_PATH` | `/var/data/skfetch-<env>` | media dataset |
| `LOCAL_ROOT` | `/var/lib/skfetch[-<env>]` | local config/SQLite |
| `LAN_BIND_ADDRESS` | `127.0.0.1` | address the *arr UIs publish on |
| `EXTRA_ALLOWED_HOSTS` | `[]` | more AllowedHosts entries |
| `BRIDGE_SUBNET` | `172.30.60.0/24` | a /24; fixed IPs `.2` `.10`-`.13` `.20` |
| `STREAM_NETWORK` | `skstream-<env>` | swarm overlay shared with Plex; `''` for none (no swarm wait) |
| `FLARESOLVERR_ENABLED` | `true` | |
| `INDEXERS` | thepiratebay, yts, nyaasi, limetorrents, Knaben, torrentdownload | Prowlarr definition names |
| `INDEXERS_FLARESOLVERR` | eztv, 1337x, uindex | tagged `flaresolverr` |
| `QBT_DL_LIMIT_KIB`, `QBT_UL_LIMIT_KIB` | `25600`, `0` | KiB/s, 0 = unlimited |
| `QBT_MAX_ACTIVE_DOWNLOADS`, `QBT_MAX_ACTIVE_UPLOADS`, `QBT_MAX_ACTIVE_TORRENTS` | `2`, `2`, `4` | |
| `QBT_MAX_RATIO`, `QBT_MAX_SEEDING_MINUTES` | `1`, `60` | then stop |
| `GLUETUN_IMAGE`, `QBITTORRENT_IMAGE`, `PROWLARR_IMAGE`, `SONARR_IMAGE`, `RADARR_IMAGE`, `LIDARR_IMAGE`, `FLARESOLVERR_IMAGE` | digest-pinned | override with another `@sha256` pin |
| `GLUETUN_MEM_LIMIT`, `QBITTORRENT_MEM_LIMIT`, `PROWLARR_MEM_LIMIT`, `SONARR_MEM_LIMIT`, `RADARR_MEM_LIMIT`, `LIDARR_MEM_LIMIT`, `FLARESOLVERR_MEM_LIMIT` | 256m, 768m, 384m, 640m x3, 512m | |
| `TRAEFIK_DYNAMIC_DIR` | `''` | e.g. the skfenceha dynamic dir; empty = no Traefik routes |
| `TRAEFIK_MIDDLEWARES` | `[default-security-headers@file, authentik@file]` | |
| `TRAEFIK_CERT_RESOLVER` | `main` | |
| `WIRE_ENABLED` | `true` | run `wire.py` after deploy |
| `WIRE_WAIT_TRIES` | `60` | x 5s for each API to answer |
| `FORCE_SEED` | `false` | rewrite qBittorrent.conf and config.xml |
| `PUID`, `PGID`, `TZ` | `1000`, `1000`, `UTC` | |

## Using it

Add a show in Sonarr, a movie in Radarr or an artist in Lidarr. Prowlarr
searches the indexers, qBittorrent downloads through the VPN into
`/data/incomplete`, the *arr app imports into `/data/media/...` and tells
Plex to scan. In Plex, add `PLEX_MEDIA_MOUNT/tv`, `/movies` and `/music` as
library folders (see `PLEX_EXTRA_BINDS` in `optional/skstream/README.md`).
