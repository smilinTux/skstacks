# skstream: Plex + Real-Debrid (DUMB)

Plex media server whose library is a Real-Debrid account mounted as a
filesystem. Titles added to the Plex watchlist are found (Torrentio), added
to Real-Debrid and appear in Plex automatically.

## Architecture

| Piece | Runs as | Where |
|---|---|---|
| Plex (`plex`) | swarm service, stack `skstream-<env>` | pinned to the media node |
| Kometa (`kometa`, optional) | swarm service | pinned to the media node |
| DUMB: zurg (RD WebDAV) + rclone FUSE mount + cli_debrid + cli_battery | host-level `skstream-dumb[-<env>].service` (docker run) | the media node |

The media node is `skstream.MEDIA_NODE`: an inventory host whose swarm node
name is the same. The deploy labels it `skstream-media=true` and pins Plex
there (`skstream.placement_constraints`).

### Why DUMB is host-level, not a swarm service

An rclone FUSE mount inside a container needs `/dev/fuse`, `SYS_ADMIN` and
`apparmor=unconfined`. A swarm service on Docker 27 can take `--cap-add`, but
has no device, `--privileged` or `--security-opt` support, so the mount cannot
work there. The same reasoning as `skbackup`'s host-level pieces: run it as a
systemd unit on the node, and keep everything that can be swarm (Plex) in
swarm, pinned to that node. Plex sees the mount through an `rslave` bind of
`/var/data/skstream-<env>/zurg/mnt` at `/mnt/debrid`.

The unit is hardened against the boot race that once took a host container
down for weeks: `ExecStartPre=/usr/local/sbin/sk-wait-swarm-net` waits for
swarm before joining the overlay, `WantedBy=docker.service` brings it back
after a docker restart, `StartLimitIntervalSec=0` never gives up, and a stale
FUSE mount is cleaned on stop and start.

### State and NFS

SQLite must not live on NFS (Plex logs "Waited over 10 seconds for a busy
database" and clients time out). So:

- Plex `Plug-in Support/Databases` binds `<LOCAL_ROOT>/plex-db` (local disk
  of the media node). Set Plex's own DB backup path to `/config/db-backups`
  (NFS) in Plex settings so a copy stays on shared storage.
- DUMB `/data` (cli_debrid DB, zurg state) and `/log` are local:
  `<LOCAL_ROOT>/dumb-data`, `<LOCAL_ROOT>/dumb-log`.
- DUMB `/config` (dumb_config.json) and Plex `/config` (metadata) are on NFS.

`LOCAL_ROOT` defaults to `/var/lib/skstream` (prod) or `/var/lib/skstream-<env>`.
Local state is not in NFS snapshots: back it up with the node.

### Seeds are written only if absent

`dumb_config.json` (NFS) and cli_debrid's `config.json` (local) are seeded on
first install only (`force: skstream.FORCE_SEED`, default false). After that
DUMB and cli_debrid own them (tokens refresh, UI edits). DUMB deep-merges its
own defaults into the seed on start. The seed enables zurg, rclone,
cli_debrid and cli_battery, and sets cli_debrid to: Plex watchlist in (every
`WATCHLIST_CHECK_MINUTES`), Torrentio, Real-Debrid, one 1080p version,
auto-run. The zurg `on_library_update` hook (`plex_update.sh`) asks Plex for
a partial scan of each changed folder; it reads the Plex token at runtime from
DUMB's config, so no secret is written into it.

## Vault keys

Namespace `skstream`. Required: `RD_API_KEY`, `PLEX_TOKEN`, `MEDIA_NODE`.

| Key | Default | Notes |
|---|---|---|
| `CLUSTERNAME`, `DOMAIN` | (required) | router host `skstream[-<env>].<CLUSTERNAME>.<DOMAIN>` |
| `INSTANCE` | `skstream-<env>` | |
| `MEDIA_NODE` | (required) | inventory host = swarm node running DUMB and Plex |
| `RD_API_KEY` | (required) | Real-Debrid API token (premium account) |
| `PLEX_TOKEN` | (required) | the Plex server's own token (Preferences.xml `PlexOnlineToken`) |
| `PLEX_CLAIM` | `''` | first install only; claims expire in minutes |
| `PLEX_ADVERTISE_URL` | `https://skstream[-<env>].<CLUSTERNAME>.<DOMAIN>:443` | |
| `TRAKT_CLIENT_ID`, `TRAKT_CLIENT_SECRET` | `''` | cli_debrid needs a Trakt app for metadata |
| `TMDB_API_KEY` | `''` | cli_debrid and Kometa |
| `CUTOFF_DATE` | `''` | watchlist cutoff: `YYYY-MM-DD` or days (e.g. `7`); `''` takes whole back catalogs of watchlisted shows |
| `WATCHLIST_CHECK_MINUTES` | `5` | |
| `RCLONE_MOUNT_NAME` | `rclone_RD` | directory under the mount; Plex libraries live below `/mnt/debrid/<name>/` |
| `PLEX_MOVIE_LIBRARIES`, `PLEX_SHOW_LIBRARIES` | `Movies`, `TV Shows` | comma lists, for cli_debrid |
| `KOMETA_ENABLED` | `false` | `KOMETA_TIME` (`03:00`) |
| `PLEX_IMAGE`, `KOMETA_IMAGE`, `DUMB_IMAGE` | digest-pinned | override with another `@sha256` pin |
| `PLEX_RESOURCES_LIMITS_CPUS`, `PLEX_RESOURCES_LIMITS_MEMORY` | `2.00`, `3G` | |
| `KOMETA_RESOURCES_LIMITS_CPUS`, `KOMETA_RESOURCES_LIMITS_MEMORY` | `1.00`, `768M` | |
| `DUMB_MEMORY_LIMIT` | `2g` | |
| `placement_constraints` | `["node.labels.skstream-media == true"]` | list; `[]` for none |
| `router_middlewares_pre`, `router_middlewares`, `tls_options` | none | Traefik router hooks |
| `PUID`, `PGID`, `TZ` | `1000`, `1000`, `UTC` | |
| `LOCAL_ROOT` | see above | |
| `FORCE_SEED` | `false` | rewrite both seeds from the vault (overwrites runtime state) |
| `networks` | skstream-prod 172.16.162.0/24 (staging .161, dev .160) | |

## Deploy

```
ansible-playbook optional/skstream/deploy_skstream-prod.yml -l <managers> -e target_manager_group=<managers>
```

## Using it

Add a movie or show to the Plex watchlist (Plex app or web: Discover, then
"+ Watchlist"). cli_debrid polls the watchlist, finds a cached 1080p release,
adds it to Real-Debrid, and the zurg hook makes Plex scan it in. Cached titles
usually appear within minutes. A whole show pulls every episode unless
`CUTOFF_DATE` limits it.
