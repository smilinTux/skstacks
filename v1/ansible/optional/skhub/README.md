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
| `TALK_HPB_IMAGE` | `ghcr.io/nextcloud-releases/aio-talk:20260122_105751` | Image for `talk-hpb` (with `enable_talk_hpb`). Pin a digest (`ghcr.io/nextcloud-releases/aio-talk:<tag>@sha256:...`). Set it to keep a newer aio-talk (newer signaling server) an instance already runs: unset, the next deploy puts talk-hpb back on the default. The TURN relay command still runs the image's own `supervisord -c /supervisord.conf`, so a replacement image must keep that path. |

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
| `TALK_TURN_RELAY_IPV4` | unset | The address eturnal advertises for relays: the public address the router forwards to the pinned node. Needs `host` mode and a relay range. aio-talk's `start.sh` always writes the container IP, so the service command rewrites that one line of `/conf/eturnal.yml` after `start.sh` and then runs the image's own `supervisord` command; if the line is missing (a changed aio-talk image) the container exits with a message instead of running a TURN server that advertises the wrong address. |

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
