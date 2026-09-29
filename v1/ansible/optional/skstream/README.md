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
| `PLEX_TOKEN` | (required) | dedicated account token from the plex.tv/link PIN flow; never the server `PlexOnlineToken` (see below) |
| `PLEX_TOKEN_IS_SERVER_TOKEN` | `false` | guard: set `true` while the vault still holds the server token and the play refuses to run |
| `PLEX_CLAIM` | `''` | first install only; claims expire in minutes |
| `PLEX_ADVERTISE_URL` | `https://skstream[-<env>].<CLUSTERNAME>.<DOMAIN>:443` | |
| `TRAKT_CLIENT_ID`, `TRAKT_CLIENT_SECRET` | `''` | cli_debrid needs a Trakt app for metadata |
| `TMDB_API_KEY` | `''` | cli_debrid and Kometa |
| `CUTOFF_DATE` | `''` | watchlist cutoff: `YYYY-MM-DD` or days (e.g. `7`); `''` takes whole back catalogs of watchlisted shows |
| `WATCHLIST_CHECK_MINUTES` | `5` | |
| `CLI_DEBRID_HYBRID_MODE` | `true` | cli_debrid `Scraping.hybrid_mode`: take a cached release if any, else the best uncached one |
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

### PLEX_TOKEN: never the server token

`PLEX_TOKEN` must be a dedicated account token, never the server `PlexOnlineToken`
(Preferences.xml). cli_debrid and Kometa use PlexAPI, which sends
`X-Plex-Provides: controller`. plex.tv binds a token to one device, so every
watchlist poll made with the server token rewrites the SERVER's plex.tv device
record into a "controller" (named after the DUMB container, versioned as
PlexAPI). Plex apps then stop listing the server and offer "Add your media /
download Plex Media Server". A server that got into that state recovers once
nothing uses its token any more and Plex Media Server is restarted (it
re-publishes its device record).

### Minting the PLEX_TOKEN

Run the plex.tv PIN (link) flow once, as its own device, and sign in with the
Plex account that owns the server:

```
CID=$(uuidgen)
H="-H X-Plex-Client-Identifier:$CID -H X-Plex-Product:SKStream-cli_debrid -H X-Plex-Device-Name:SKStream-cli_debrid -H Accept:application/json"
curl -s -X POST $H "https://plex.tv/api/v2/pins?strong=false"   # note "id" and 4-char "code"
# open https://plex.tv/link while signed in, enter the code
curl -s $H "https://plex.tv/api/v2/pins/<id>"                    # "authToken" is PLEX_TOKEN
```

Keep the same client identifier for that device; store the token in the vault
as `skstream.PLEX_TOKEN`. The new device shows in plex.tv account devices as
"SKStream-cli_debrid" and can be revoked there without touching the server.
The seeds are written only if absent, so on an existing install also replace
the token in `dumb_config.json` (`dumb.plex_token`) and cli_debrid
`config.json` (`Plex.token`, `File Management.plex_token_for_symlink`), or
redeploy once with `FORCE_SEED: true`.

### Real-Debrid limits worth knowing

- RD removed its instant-availability API, so cli_debrid cannot tell cached
  from uncached ahead of time. With uncached handling `None` and hybrid mode
  off, older shows are blacklisted wholesale because nothing is known to be
  cached. Hybrid mode (default on) takes a cached release if there is one and
  otherwise the best uncached release.
- A release RD reports as 100% downloaded can still be DMCA-blocked: unrestrict
  fails with HTTP 451 `infringing_file`. cli_debrid cannot see this before it
  adds the torrent; the item fails and is retried with another release.

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
