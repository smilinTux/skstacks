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

### Services reached from host-level containers: use dnsrr

Host-level containers on a shared attachable swarm overlay (skstream DUMB,
skfetch's *arr apps) get their IPs from the local docker daemon, which does
not know which addresses swarm holds as service VIPs. After a full-cluster
reboot one of them was handed the Plex service VIP: "plex" resolved to an
address owned by another container, calls were black-holed for hours, and
restarting the container left stale IPVS routing behind. Any swarm service
that host-level containers reach by name should therefore run with
`deploy.endpoint_mode: dnsrr` (the name resolves to task IPs, no VIP). It
needs no published ports; Traefik already targets task IPs. skstream does
this for Plex by default (`PLEX_ENDPOINT_MODE`).

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
| `CLI_DEBRID_FILTER_OUT` | Russian-release filter | list of cli_debrid `filter_out` patterns added to each managed scraping version on every deploy (append only: patterns added in the UI stay, and removing one here does not remove it there). Unset uses `DEFAULT_FILTER_OUT` in `skstream-converge`, which rejects `Rus`/`Ru`/`Ukr` tags, Russian dub and tracker groups (LostFilm, ColdFilm, ViruseProject, Ultradox and others) and any Cyrillic letter in a release name. `language_code: en` alone only lowers a foreign release's score, so a cached Russian pack can still win. `[]` adds nothing |
| `CLI_DEBRID_FOREIGN_LANGUAGE_PENALTY` | `1000` | cli_debrid `foreign_language_penalty` on each managed scraping version, set on every deploy. With `language_weight` 3 a release tagged with another audio language (cli_debrid's own detection; subtitle tags, MULTi/DUAL/DL and the title's original language are exempt) scores about -2350, so together with `CLI_DEBRID_MINIMUM_SCRAPE_SCORE` it is dropped, not just ranked down. That matters because the Adding queue takes any cached release before any uncached one whatever its score. cli_debrid's stock value is `30` |
| `CLI_DEBRID_MINIMUM_SCRAPE_SCORE` | `1.0` | cli_debrid `Scraping.minimum_scrape_score`, set on every deploy; global, drops every result scoring below it. Real releases scored 483-953 on nor. `0.0` turns the filter off (and with it the drop above) |
| `RCLONE_MOUNT_NAME` | `rclone_RD` | directory under the mount; Plex libraries live below `/mnt/debrid/<name>/` |
| `PLEX_MOVIE_LIBRARIES`, `PLEX_SHOW_LIBRARIES` | `Movies`, `TV Shows` | comma lists, for cli_debrid |
| `KOMETA_ENABLED` | `false` | `KOMETA_TIME` (`03:00`) |
| `PLEX_IMAGE`, `KOMETA_IMAGE`, `DUMB_IMAGE` | digest-pinned | override with another `@sha256` pin |
| `PLEX_RESOURCES_LIMITS_CPUS`, `PLEX_RESOURCES_LIMITS_MEMORY` | `2.00`, `3G` | |
| `KOMETA_RESOURCES_LIMITS_CPUS`, `KOMETA_RESOURCES_LIMITS_MEMORY` | `1.00`, `768M` | |
| `DUMB_MEMORY_LIMIT` | `2g` | |
| `PLEX_ENDPOINT_MODE` | `dnsrr` | `vip` or `dnsrr`; keep dnsrr while host-level containers share the overlay (see above) |
| `PLEX_EXTRA_BINDS` | `[]` | list of `{source, target}` read-only binds into Plex, e.g. `{source: /var/data/skfetch-prod/media, target: /skfetch/media}` so libraries can include skfetch folders |
| `DUMB_EXTRA_BINDS` | `[]` | list of `{source, target, read_only}` binds into the DUMB container (`read_only` default `true`), rendered as `-v source:target[:ro]` after the `/data` bind. The source file is yours to place on `MEDIA_NODE`; absolute paths without spaces or colons (the play checks). The play refuses a bind over `/cli_debrid/content_checkers/plex_watchlist.py` while `CLI_DEBRID_VERSION` pins a release: the managed Plex Home fix it once carried is upstream now |
| `CLI_DEBRID_VERSION` | `v0.7.81` | cli_debrid release DUMB installs (set in `dumb_config.json` on every deploy; a change restarts `skstream-dumb`). `''` leaves DUMB's own selection alone. DUMB images up to at least 2.23.0 bake cli_debrid 0.7.23, and the container runs with `--rm`, so a pin is downloaded from GitHub on each start of `skstream-dumb`. A pin also sets `cli_battery.enabled: false`: cli_debrid v0.7.29 and later run CLI Battery in-process and ship no `cli_battery/main.py`, so DUMB's standalone battery service crashes and holds cli_debrid back. It also points cli_debrid's data paths (`USER_CONFIG`, `USER_LOGS`, `USER_DB_CONTENT`, `config_file`, `log_file`) at `/data/cli_debrid` directly: DUMB reinstalls a pinned release into `/cli_debrid` on every start and its `/cli_debrid/data` symlink did not survive that. Drop the pin once a `DUMB_IMAGE` bakes the release you want |
| `PLEX_OTHER_WATCHLISTS` | `[]` | list of `{username, token, display_name}`: one "Other Plex Watchlist" source per viewer, each with that viewer's own plex.tv token. `username` is the plex.tv username, or the profile title for a managed Plex Home user. Tokens are secrets: vault only (see [Viewers](#viewers-and-their-watchlists)) |
| `PLEX_FRIENDS_WATCHLIST` | `false` | add one "My Friends Plex RSS Watchlist" source fed by the owner's friends RSS feed (needs Plex Pass on the `PLEX_TOKEN` account); the play fetches the feed URL |
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

## Viewers and their watchlists

Every viewer's watchlist feeds cli_debrid the same way the owner's does, once it
is a content source. Which kind depends on the account:

- **Managed Plex Home profile** (a kid's profile, no plex.tv login of its own):
  an entry in `PLEX_OTHER_WATCHLISTS` with the profile title as `username` and
  the profile's own plex.tv token. The owner can mint it: cli_debrid's
  Settings, "Collect User Tokens", or plexapi
  `MyPlexAccount(token=OWNER).switchHomeUser(title).authenticationToken`.
- **Friend** (their own plex.tv account, the server shared with them): the
  owner cannot mint their token. `PLEX_FRIENDS_WATCHLIST: true` covers every
  friend at once through the owner's friends RSS feed, as long as the friend's
  watchlist privacy lets friends see it. A friend who hands over their own
  token can go in `PLEX_OTHER_WATCHLISTS` instead.

The seeds are written once, so these do not live in the seed: every deploy
runs `skstream-converge` on `MEDIA_NODE`, which upserts exactly these sources
(matched by type and username), their check period
(`WATCHLIST_CHECK_MINUTES`) and the `CLI_DEBRID_VERSION` pin. It never removes
or edits a source it did not create, and prints only what changed. When only
sources changed it restarts cli_debrid alone through DUMB's API, so the rclone
mount Plex reads stays up.

Library access is plex.tv account state, not deploy state: give a new viewer
the same libraries and Downloads as the others in Plex (Settings, Manage
Library Access) or with plexapi `updateFriend(user, server,
sections=server.library.sections())`.

## Using it

Plex libraries: add `/mnt/debrid/<RCLONE_MOUNT_NAME>/...` folders for the
debrid side and, with `PLEX_EXTRA_BINDS`, the extra mount (for skfetch:
`/skfetch/media/tv`, `/movies`, `/music`). Create a Music library too
before wiring Lidarr: Lidarr refuses a Plex connection until one exists.

Add a movie or show to the Plex watchlist (Plex app or web: Discover, then
"+ Watchlist"). cli_debrid polls the watchlist, finds a cached 1080p release,
adds it to Real-Debrid, and the zurg hook makes Plex scan it in. Cached titles
usually appear within minutes. A whole show pulls every episode unless
`CUTOFF_DATE` limits it.
