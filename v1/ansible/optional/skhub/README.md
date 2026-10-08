# skhub (Nextcloud)

Nextcloud plus its supporting services (MariaDB, Redis, ClamAV, cron,
notify_push, whiteboard, imaginary, and optional Collabora / Talk HPB) on
Docker Swarm.

## Storage backend

`skhub.storage_backend` controls where Nextcloud's file data lives:

| Value | Behaviour |
|---|---|
| unset / `local` (default) | Files live on `/var/data/skhub-<env>/data`, this instance's shared filesystem (NFS), matching production. No extra vault keys needed. |
| `s3` | Nextcloud's primary storage is an external S3-compatible object store you point at yourself: `skhub.s3_host`, `skhub.s3_access_key`, `skhub.s3_secret_key`, plus optional `skhub.s3_bucket_name` / `s3_port` / `s3_ssl` / `s3_usepath_style` / `s3_autocreate`. |
| `skstor` | A shortcut for `s3` against this cluster's own in-cluster [skstor](../skstor/README.md) (Garage) service. Host/port/region default to the in-cluster service (`skstor-<env>-garage`, port `3900`, no TLS, region `garage`) and this stack joins the `skstor-<env>` overlay network automatically. You still need `skhub.s3_access_key` / `skhub.s3_secret_key` from skstor's own one-time `garage key create` / `garage bucket allow` bootstrap (see skstor's README). |

**skstor must already be deployed** before you deploy skhub with
`storage_backend: skstor` -- its overlay network and Garage service have to
exist for skhub's compose to join them and reach it.

**Switching an existing instance is not supported by this playbook.**
Nextcloud reads `OBJECTSTORE_S3_*` only on first install (`occ` bootstrap);
changing `skhub.storage_backend` on a running instance does not migrate
already-uploaded files and will not be picked up without Nextcloud's own
`occ files:transfer-ownership`-style S3 migration tooling, which this
framework does not run. Set the backend before the first deploy of a new
instance.

## Minimal example: skhub with skstor as primary storage

```yaml
# skhub vault, in addition to the usual nextcloud_admin_*/mysql_* keys
skhub:
  storage_backend: skstor
  s3_access_key: "<from garage key create>"
  s3_secret_key: "<from garage key create>"
  # s3_bucket_name, s3_host, s3_port, s3_ssl, s3_usepath_style, s3_region
  # all default correctly for the in-cluster skstor service; override only
  # if you renamed the bucket or moved skstor off its defaults.
```

## Nextcloud image

| Key | Default | Meaning |
|---|---|---|
| `NEXTCLOUD_IMAGE` | `nextcloud:31.0.14` | Image for `nextcloud`, `cron` and `notify_push` (they share `/var/www/html`, so always the same image). Pin a digest (`nextcloud:<ver>@sha256:...`). Nextcloud upgrades are one-way and one major at a time: once an instance runs a newer Nextcloud, keep this set, because deploying an older image makes the entrypoint refuse to start (downgrade) and the service crash-loops. |
| `TALK_HPB_IMAGE` | `ghcr.io/nextcloud-releases/aio-talk:latest@sha256:831a08f9772d188e65aee2fb59ef3785d3567a1fe2fb65f0e6fd2c17a16ffc87` (v2.26.3; before: `aio-talk:20260122_105751`, whose `start.sh` ignores `TURN_DOMAIN` and writes no TURN server into `janus.jcfg`, so Janus offered only its container address and calls outside the LAN failed ICE) | Image for `talk-hpb` (with `enable_talk_hpb`). Pin a digest (`ghcr.io/nextcloud-releases/aio-talk:<tag>@sha256:...`). Set it to keep a newer aio-talk (newer signaling server) an instance already runs: unset, the next deploy puts talk-hpb back on the default. With `TALK_TURN_RELAY_IPV4` set, the relay command runs `TALK_HPB_CMD` (default `supervisord -c /supervisord.conf`, the default image's CMD); a newer aio-talk that runs dinit needs `TALK_HPB_CMD` set to its own CMD. |
| `TALK_HPB_CMD` | the image's own CMD: `[dinit, --system, --container, nats-server, eturnal, janus, signaling]` for the default image; `[supervisord, -c, /supervisord.conf]` when `TALK_HPB_IMAGE` is set (the pre-v2.26.3 default) | A list: the command the TURN relay wrapper execs after rewriting `relay_ipv4_addr` (only with `TALK_TURN_RELAY_IPV4`). Set it to the `TALK_HPB_IMAGE`'s own CMD (`docker image inspect -f '{{json .Config.Cmd}}'`), e.g. `[dinit, --system, --container, nats-server, eturnal, janus, signaling]` for aio-talk images that dropped supervisord. If its first word is not in the image the container exits 127 with a message naming this knob. |
| `MARIADB_IMAGE` | `mariadb:10.11.19@sha256:7db29378d4fdab73f8123bbc2b48905c90d1a4b00cf848b028f1e81e623257f2` | Image for `db` and `db-backup`. Stays on the 10.11 LTS line; a major jump (e.g. to 11.x) is a data migration this framework does not perform, so set this only to a newer 10.11.x build. |
| `REDIS_IMAGE` | `redis:8.10.2-alpine@sha256:3811787313eba226a2ef38658c6ccb91cd5e110edc89c37767de373120a0e5a0` | Image for `redis`. Stays on the 8.x line for the same reason as `MARIADB_IMAGE`. |
| `CLAMAV_IMAGE` | `clamav/clamav:latest@sha256:ebec5bc138401b36ae987caa1a3fa3c3b2a21ed3d51f0bfa5852825e663e67b0` | Image for `clamav`. Stateless (signature database only), so tracking upstream `latest` and re-pinning the digest here is fine. |
| `COLLABORA_IMAGE` | `collabora/code:26.04.4.2.1@sha256:4e983196eb9878f339cc506c38c21f1cc3473bca3d6de883c5de08f9c0cc3a6c` | Image for `collabora` (with `enable_collabora`). Stateless; tracking upstream `latest` is fine. |
| `WHITEBOARD_IMAGE` | `ghcr.io/nextcloud-releases/whiteboard:v2.0.0@sha256:059596633333d009890c64858f89f133ccd973a2e0823ad960a0cc05cf8657f1` | Image for `whiteboard`. Keep this in step with the installed `whiteboard` Nextcloud app version (`occ app:list`): the v2.0.0 backend speaks a different protocol/auth handshake than v1.x and an instance whose app is already on 2.x will not work correctly against an older backend. |
| `IMAGINARY_IMAGE` | `nextcloud/aio-imaginary:latest@sha256:15d3b439849a675d7b646db6796654ae238dd9c84ea089258d19c2fa69999cdf` | Image for `imaginary`. Stateless; tracking upstream `latest` is fine. |

## ClamAV tuning

The `clamav/clamav` image has no env var for `clamd.conf`'s `MaxThreads`,
`StreamMaxLength`, `MaxScanSize` or `ConcurrentDatabaseReload` (only
`CLAMAV_NO_CLAMD`/`CLAMAV_NO_FRESHCLAMD`/`CLAMAV_NO_MILTERD`/
`CLAMD_STARTUP_TIMEOUT`/`FRESHCLAM_CHECKS` are env vars); the documented way to
change them is a full bind-mount over `/etc/clamav/clamd.conf`, which is what
`clamd.conf.j2` does.

Measured on a throwaway local `clamav/clamav:latest` container (not a cluster):
with the stock config (`MaxThreads` 10, `ConcurrentDatabaseReload` yes, both
compiled-in defaults), idle RSS was ~961 MiB and a forced `clamdscan --reload`
spiked to ~1.86 GiB (+94%) before settling back, because the old and new
signature database are both resident during the reload. With
`ConcurrentDatabaseReload no` (the old database is freed before the new one
loads), idle RSS was ~956 MiB (unchanged: that is mostly the resident
signature database) and the same reload never exceeded the idle baseline (it
dipped to ~240 MiB then climbed back to ~954 MiB). `MaxThreads` bounds
worst-case *concurrent*-scan memory, which an idle+reload test does not
exercise.

| Key | Default | Meaning |
|---|---|---|
| `CLAMD_MAX_THREADS` | `2` | `clamd.conf` `MaxThreads`. The image default is 10; most skhub instances only ever have Nextcloud's own `files_antivirus` app talking to this one daemon. |
| `CLAMD_STREAM_MAX_LENGTH_MB` | `25` | `clamd.conf` `StreamMaxLength` (megabytes). Also drives `files_antivirus`'s `av_stream_max_length` (see below), so the two cannot drift apart. |
| `CLAMD_MAX_SCAN_SIZE_MB` | `100` | `clamd.conf` `MaxScanSize` (megabytes). |
| `CLAMD_CONCURRENT_DATABASE_RELOAD` | `false` (renders `no`) | `clamd.conf` `ConcurrentDatabaseReload`. `true` restores the image's compiled-in default (~94% RSS spike during a database reload, measured above). |
| `CLAMAV_RESOURCES_RESERVATIONS_MEMORY` | `512M` | `clamav` service `deploy.resources.reservations.memory`. |
| `CLAMAV_RESOURCES_LIMITS_MEMORY` | `2G` | `clamav` service `deploy.resources.limits.memory`. Comfortably above the ~1.86 GiB untuned-reload peak measured above; with the tuned defaults actual usage should not exceed the ~960 MiB idle baseline. |
| `enable_files_antivirus` | `false` | When `true`, the deploy additionally sets the `files_antivirus` app's `av_max_file_size` (`FILES_ANTIVIRUS_MAX_FILE_SIZE`, default `-1` unlimited) and `av_stream_max_length` (bytes, computed from `CLAMD_STREAM_MAX_LENGTH_MB`) via `occ`. Unset/false on an instance that never installed `files_antivirus` (e.g. the clamav container is unused), so the deploy does not write appconfig rows for an app that is not there. This is in addition to the existing unconditional `av_host`/`av_port`/`av_mode` task, which always runs. |
| `FILES_ANTIVIRUS_MAX_FILE_SIZE` | `-1` | `files_antivirus` app's `av_max_file_size` (bytes, `-1` = unlimited), only applied with `enable_files_antivirus`. |

## Local code tree on a pinned node

By default the Nextcloud code tree, `/var/www/html` (about 30k files) and
`custom_apps` (about 60k files, several GB with Recognize's models), is bind
mounted from `/var/data/skhub-<env>/`, which is NFS on a typical instance.
Every request and every `occ` command stats thousands of those small files: on
one production instance `occ integrity:check-core` took 340 s and the admin
Overview setup checks timed out. Worse, **a Nextcloud image bump makes the
entrypoint rsync the whole code tree onto that mount** (`rsync --delete` of
about 700 MB of small files), which over NFS took 35 to 75 minutes per hop
while the service sat unhealthy. A `docker service update` of anything on the
service during that time recreates the task and restarts the copy. Keep the
code tree on local disk before the next upgrade.

| Key | Default | Meaning |
|---|---|---|
| `WEBROOT_LOCAL_PATH` | empty | A local directory on `APP_NODE` holding `html/` and `custom_apps/`. `nextcloud` and `cron` mount `<path>/html` and `<path>/custom_apps`, `notify_push` mounts `<path>/custom_apps` read-only. `config`, `data` and `themes` stay on `/var/data`. Empty renders exactly as before. Must be absolute, outside `/var/data`, no trailing slash. |
| `APP_NODE` | empty | Node hostname: adds `node.hostname == <APP_NODE>` to `nextcloud`, `cron` and `notify_push` only (after `placement_constraints`). Required with `WEBROOT_LOCAL_PATH`: the play stops without it, because a local tree on a floating service is an empty directory after the next reschedule, which the entrypoint treats as a fresh install. |
| `WEBROOT_NFS_SYNC` | `true` | With a local path: a systemd timer on `APP_NODE` (`skhub-<env>-webroot-sync.timer`) copies the local tree back to `/var/data/skhub-<env>/{html,custom_apps}` (`rsync --delete`, `ionice -c3`, bandwidth limited), so the shared copy stays a usable failover source and rollback path. It refuses to run when the local tree has no `html/version.php` or an empty `custom_apps`, or when the shared directories are missing (storage not mounted). `false` installs nothing (an existing timer is left alone: `systemctl disable --now` it by hand). |
| `WEBROOT_NFS_SYNC_ON_CALENDAR` | `*-*-* 03:17:00 UTC` | systemd `OnCalendar` for the sync (with up to 10 minutes of random delay, `Persistent=true`). |
| `WEBROOT_NFS_SYNC_BWLIMIT` | `20000` | rsync `--bwlimit` in KiB/s. After an image bump the first sync writes the whole new code tree to NFS. |

The deploy creates `<path>`, `<path>/html` and `<path>/custom_apps` on
`APP_NODE` (not recursive), and stops if `<path>/html/version.php` is missing
there while `/var/data/skhub-<env>/html` holds an installed Nextcloud (an
unseeded local tree). A new instance with nothing on `/var/data` deploys
straight onto local disk.

Trade-off: the three services can no longer move to another node on their
own. If `APP_NODE` dies, point `APP_NODE` at another node, seed that node from
the shared copy (step 3 below) and redeploy, or follow the rollback.

### Migrating a running instance

Plan a short maintenance window (minutes; the copy is done beforehand). Take
your usual pre-change backup (a database dump and a storage snapshot) first.

1. Pick `APP_NODE` (normally the node already running `nextcloud`) and check
   its local free space against `du -sh /var/data/skhub-<env>/{html,custom_apps}`.
2. Pre-seed while the site is up (heavy reads on shared storage: throttle it):
   ```
   install -d -m 0755 <path> && install -d -o www-data -g www-data -m 0755 <path>/html <path>/custom_apps
   ionice -c3 rsync -aHAX --numeric-ids --delete --bwlimit=40000 \
     --exclude='/data/**' --exclude='/config/**' --exclude='/custom_apps/**' --exclude='/themes/**' \
     /var/data/skhub-<env>/html/ <path>/html/
   ionice -c3 rsync -aHAX --numeric-ids --delete --bwlimit=40000 \
     /var/data/skhub-<env>/custom_apps/ <path>/custom_apps/
   ```
3. `occ maintenance:mode --on`, then scale `skhub-<env>_nextcloud`, `_cron`
   and `_notify_push` to 0 so nothing writes the tree, and run the same two
   rsyncs again (only the delta). Compare file counts and byte totals of both
   sides before going on.
4. Set `WEBROOT_LOCAL_PATH` and `APP_NODE` in the vault and deploy skhub, or,
   to change only the live services, update each one with its replicas still
   at 0. **Never `--mount-rm X` and `--mount-add ...target=X` in one
   `docker service update`**: docker drops both and the task starts on the
   image's anonymous `/var/www/html` volume. Remove in one update, add (plus
   `--constraint-add node.hostname==<APP_NODE>`) in a second.
5. Scale `nextcloud` to 1 and wait until it is healthy (`occ status`
   reports the same version: no upgrade copy runs, the tree is already in
   place), then `cron` and `notify_push`, then `occ maintenance:mode --off`.
6. Verify `status.php`, a login, WebDAV, a cron run and
   `occ notify_push:self-test`, and run the sync once by hand:
   `systemctl start skhub-<env>-webroot-sync.service`.

Rollback: `occ maintenance:mode --on`, scale the three services to 0, make
sure the shared copy is current (run the sync service if the local disk is
still readable), swap the mounts back to `/var/data/skhub-<env>/{html,custom_apps}`
and drop the hostname constraint (again in separate updates, or clear the two
keys and redeploy), scale back up and turn maintenance off.

## Public hostnames

Two keys set the public names Nextcloud and Collabora answer on:

```yaml
skhub:
  SKHUB_HOSTNAME: cloud.example.com        # the Nextcloud host users open
  COLLABORA_HOSTNAME: office.example.com   # with enable_collabora
```

| Key | Default | Used for |
|---|---|---|
| `SKHUB_HOSTNAME` | `skhub[-dev\|-staging].<base domain>` | The Traefik routers (Nextcloud, notify_push, whiteboard, Talk HPB), `OVERWRITEHOST`/`TRUSTED_DOMAINS`, notify_push's `overwritehost`, and the post-deploy settings `notify_push:setup` and notify_push `base_endpoint` (`https://<host>/push`), whiteboard `collabBackendUrl` (`wss://<host>/whiteboard`) and Talk `spreed` `signaling_servers` (`https://<host>/standalone-signaling/`). |
| `COLLABORA_HOSTNAME` | `collabora.<base domain>` | The Collabora routers, `NEXTCLOUD_RICHODOCUMENTS_CODE_URL` and the post-deploy richdocuments `wopi_url` (`https://<host>`). |

`<base domain>` is `<DOMAIN>` with `CLOUDFLARED: true` and
`<CLUSTERNAME>.<DOMAIN>` without. Before v2.26.3 the five post-deploy settings
were hardcoded to `skhub.`/`collabora.<CLUSTERNAME>.<DOMAIN>`, so on any
instance where that differed from the routers' host (`CLOUDFLARED`, a dev or
staging env, or a set key) every deploy rewrote them to names the routers do
not answer on: Talk reported the HPB as "Unknown error", and push,
whiteboard and Collabora stopped working. They now always match the routers.
The `talk-hpb` env (`NC_DOMAIN`, `TALK_HOST`) and the Collabora/whiteboard env
files still build the host from `<base domain>` only and do not read these
two keys.

## Local database on a pinned node

By default `db` (MariaDB) and `redis` keep their data under
`/var/data/runtime` (shared storage, NFS on a typical instance) and float
across workers. NFS write latency there (seen as high as ~124 ms RTT on one
instance) shows up as slow `occ` capability checks and Talk HPB backend
timeouts. To keep them on local disk on one node:

```yaml
skhub:
  DATA_NODE: <node hostname>               # also an inventory host
  DB_DATA_PATH: /var/lib/skhub-<env>/db
  REDIS_DATA_PATH: /var/lib/skhub-<env>/redis
```

| Key | Default | Meaning |
|---|---|---|
| `DATA_NODE` | empty | Node hostname: adds `node.hostname == <DATA_NODE>` to `db` and `redis` only (after `placement_constraints`). Required with a local `DB_DATA_PATH`/`REDIS_DATA_PATH`: the play stops without it, because local data on a floating service is an empty database after the next reschedule. `db-backup`, `clamav`, `imaginary`, `collabora` and the app services keep floating. |
| `DB_DATA_PATH` | `/var/data/runtime/skhub-<env>/db` | MariaDB datadir (`/var/lib/mysql`). A path outside `/var/data` is local disk on `DATA_NODE`. |
| `REDIS_DATA_PATH` | `/var/data/runtime/skhub-<env>/redis` | Redis `/data`. Same rule. |

A path outside `/var/data` is created on `DATA_NODE` (not recursive, same
owner/mode as the shared-storage copy: `db` root:root 0755, `redis`
root:root 0777, since redis runs as root in the container and forks
background-save children) and the old `/var/data/runtime` path is no longer
created. Local data is not on shared storage: back it up with the node, and
keep the nightly `database-dump/` dump on shared storage as it is.

### Migrating a running instance

1. Pick `DATA_NODE` (normally the node already running `db`/`redis`) and
   check its local free space against `du -sh /var/data/runtime/skhub-<env>/{db,redis}`.
2. `occ maintenance:mode --on`, scale `db` and `redis` to 0, and wait until
   no task is running.
3. On `DATA_NODE`: `rsync -aHAX --numeric-ids /var/data/runtime/skhub-<env>/db/
   <path>/db/` (same for redis). Verify: file counts and total sizes match on
   both sides (`find <dir> | wc -l`, `du -s --apparent-size`).
4. Swap the mounts to `<path>/db` and `<path>/redis` (a `docker service
   update --mount-rm ... --mount-add ...` per service, plus `--constraint-add
   node.hostname==<DATA_NODE>`), scale back to 1, verify a db ping and
   `occ maintenance:mode --off`.
5. Rename the old NFS dirs (for example `db.STALE-moved-to-<node>-local-<date>`)
   so a future deploy from a template that still hardcodes the old path fails
   loudly (no directory to bind-mount) instead of starting on stale data, and
   leave them in place as the rollback copy. Never delete them.
6. Set the three vault keys (`DATA_NODE`, `DB_DATA_PATH`, `REDIS_DATA_PATH`)
   and deploy skhub so the next redeploy renders the same mounts instead of
   reverting them.

**WARNING:** do not switch a mount by hand with one
`docker service update --mount-rm <target> --mount-add type=bind,src=...,target=<target>`.
`--mount-rm` is applied last, so it removes BOTH the old and the new mount
for that target, and the container starts on an empty anonymous volume. Do
the `--mount-rm` and `--mount-add` in separate updates, or (better) redeploy
the stack from the template.

## Talk HPB TURN

With `skhub.enable_talk_hpb: true`, the `talk-hpb` service (aio-talk) runs
Janus, the signaling server and an eturnal TURN/STUN server on 3478 tcp+udp,
using the shared secret `skhub.turn_secret`. By default 3478 is published
through the Swarm ingress. That is enough for Talk itself (Janus, the only
peer it relays to, is in the same container), but not for callers or other
clients that need a real TURN server from outside the LAN:

- through the ingress eturnal sees the ingress SNAT address, so a STUN binding
  answers an internal `10.0.0.x` address, and a TURN client's permission for
  its own public address is refused as a private peer;
- eturnal advertises the container IP as its relay address, and the relay
  ports are not published.

Optional keys (all unset = the ingress behaviour above, rendered unchanged):

| Key | Default | Meaning |
|---|---|---|
| `TALK_TURN_PUBLISH_MODE` | `ingress` | `host` publishes 3478 tcp+udp in host mode, so eturnal sees real client addresses. |
| `TALK_HPB_PLACEMENT_CONSTRAINTS` | `placement_constraints` | talk-hpb's own placement. Required with `host`: host-mode ports open only on the node running the task, and that node is where the router must forward (for example `["node.hostname == <node>"]`). |
| `TALK_TURN_RELAY_MIN_PORT`, `TALK_TURN_RELAY_MAX_PORT` | unset | A bounded relay port range, `host` mode only, at most 200 ports, 1024-65535. Passed to eturnal as `ETURNAL_RELAY_MIN_PORT`/`ETURNAL_RELAY_MAX_PORT` and published udp in host mode. Pick it outside the node's ephemeral range (`/proc/sys/net/ipv4/ip_local_port_range`, usually 32768-60999), since every port is bound on the host. |
| `TALK_TURN_RELAY_IPV4` | unset | The address eturnal advertises for relays: the public address the router forwards to the pinned node. Needs `host` mode and a relay range. aio-talk's `start.sh` always writes the container IP, so the service command rewrites that one line of `/conf/eturnal.yml` after `start.sh` and then runs `TALK_HPB_CMD` (the image's own CMD); if the line is missing (a changed aio-talk image) the container exits with a message instead of running a TURN server that advertises the wrong address. |
| `TURN_DOMAIN` | unset (renders `{{ CLUSTERNAME }}.{{ DOMAIN }}`, same as before this knob existed) | Overrides only the hostname Nextcloud's `turn_servers` config advertises to clients; does not change `talk-hpb`/eturnal at all. For an instance whose own ingress cannot serve TURN (inbound 3478 blocked at the ISP, for example) and that instead relays Talk's TURN/STUN through another cluster's TURN server: set `TURN_DOMAIN` to that cluster's TURN hostname and `turn_secret` to its secret; `talk-hpb`'s own eturnal then runs unused. The port (3478) and `turn_secret` are unaffected by this knob. |

The deploy playbooks refuse an inconsistent combination before rendering.

Sizing the range: every TURN allocation holds one relay port for its
lifetime. A browser allocates per peer connection and per TURN URL (Talk
lists udp and tcp), and with the HPB each client holds one connection to
publish plus one per other participant, so a call of N clients all outside
the LAN needs up to about 2 x N x N ports: 100 ports covers about 7, 200 about
10. Each port is a host-mode mapping (iptables rules plus a `docker-proxy`
process of about 1 MB with the default userland proxy).

Router forwards (all to the pinned node's LAN address): 3478 tcp and udp, and
the relay range udp. Talk's own TURN settings (`turn,turns` at
`skhub.<base>:3478`, set by the post-deploy task) do not change. Other
clients may use the same server with credentials minted from
`skhub.turn_secret` (TURN REST scheme: user `<expiry>:<name>`, password
base64 HMAC-SHA1).

Example:

```yaml
skhub:
  enable_talk_hpb: true
  TALK_TURN_PUBLISH_MODE: host
  TALK_HPB_PLACEMENT_CONSTRAINTS: ["node.hostname == worker3"]
  TALK_TURN_RELAY_MIN_PORT: 62000
  TALK_TURN_RELAY_MAX_PORT: 62099
  TALK_TURN_RELAY_IPV4: 203.0.113.10
```

## Talk recording and local AI

Both default OFF: an instance that does not set these keys renders and
deploys exactly as before.

### Talk recording

With `skhub.TALK_RECORDING_ENABLED: true` a `talk-recording` service (the
official `ghcr.io/nextcloud-releases/aio-talk-recording` image: ffmpeg plus a
headless Firefox, about 1-2 CPU cores per active recording) joins the stack
and is registered with Talk (`occ config:app:set spreed recording_servers`).
Recordings land in the call starter's Talk folder (Talk's own default).

| Key | Default | Meaning |
|---|---|---|
| `TALK_RECORDING_ENABLED` | `false` | Adds the `talk-recording` service and registers it with Talk. |
| `TALK_RECORDING_IMAGE` | `ghcr.io/nextcloud-releases/aio-talk-recording:20260929_105435@sha256:64aa51b0279a4ab5c16249ad1e8f5e56b572ffee3947f14b64386429bc9c6696` | Pin a digest (`docker buildx imagetools inspect ghcr.io/nextcloud-releases/aio-talk-recording:<tag>`, or the registry API). |
| `TALK_RECORDING_NODE` | none, required when enabled | Pins `talk-recording` (`node.hostname == <node>`). **Must differ from `APP_NODE`**: recording CPU must not compete with Nextcloud's own node; the deploy refuses the combination. |
| `TALK_RECORDING_SECRET` | none, required when enabled | Shared secret (vault, 32+ characters) between Nextcloud and the recording backend (`RECORDING_SECRET` on the container, `secret` in Talk's `recording_servers`). Generate with `openssl rand -base64 32`. |
| `TALK_RECORDING_MAX_CONCURRENT` | `2` | `talk-recording` replica count: capacity for this many simultaneous recordings, one independent ffmpeg+browser worker per replica. This is provisioned capacity, not admission control -- neither Talk nor the recording server has a native concurrency limit, and Swarm's VIP round-robins per connection, so an (N+1)th recording is not refused, it lands on an already-busy worker instead of failing. |

`talk-recording` is not published externally: Nextcloud reaches it over the
`skhub-<env>` overlay network at `http://<app>-<env>_talk-recording:1234`
(the service name Swarm resolves internally, same pattern as `av_host` for
ClamAV). It also joins `cloud-public-<env>` (like `talk-hpb`), because the
container calls back out to Nextcloud and the signaling server at their
public hostname.

### Local AI (integration_openai + assistant)

With `skhub.AI_ENABLED: true` the deploy installs and enables the
`integration_openai` and `assistant` apps and points `integration_openai` at
an OpenAI-compatible gateway (built for [skgateway](https://github.com/smilinTux/skgateway),
but any compatible endpoint works) so Assistant chat, summarize, rewrite,
headline, translate and speech-to-text run against your own models instead of
OpenAI's cloud.

| Key | Default | Meaning |
|---|---|---|
| `AI_ENABLED` | `false` | Installs/enables `integration_openai` + `assistant` and configures the provider. |
| `AI_BASE_URL` | none, required when enabled | The gateway's OpenAI-compatible base URL (e.g. `http://gw.example:18780/v1`). Must start with `http://` or `https://`. |
| `AI_API_KEY` | none, required when enabled (vault) | The gateway consumer key for this instance. |
| `AI_TEXT_MODEL` | `sk-default` | `integration_openai` `default_completion_model_id`: a gateway **model alias**, never a literal cloud model id -- the gateway decides where it actually runs. |
| `AI_STT_MODEL` | `sk-stt` | `integration_openai` `default_stt_model_id`, same alias convention. |

Image generation and text-to-speech are out of scope here (both default on
in `integration_openai` and use hardcoded OpenAI model ids,
`gpt-image-1-mini` and `tts-1-hd`, that a local gateway does not serve), so
the deploy turns both off (`t2i_provider_enabled`, `tts_provider_enabled`).
The vision provider (`analyze_image_provider_enabled`) needs no separate
call: `integration_openai` only defaults it on while `url` is still OpenAI's
own cloud endpoint, so pointing `url` at the gateway already turns it off.
