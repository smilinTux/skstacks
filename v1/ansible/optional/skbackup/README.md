# skbackup

Host-level backups for the storage host that serves a cluster's data, and the
framework's backup pattern doc. skbackup is **not a swarm stack**: it is an
Ansible playbook (`deploy_skbackup-<env>.yml`) that targets a storage-host
inventory group (a Proxmox node or any Linux host that exports the swarm's
`/var/data` over NFS from a **ZFS dataset**) and installs there:

- a small bash engine (`/usr/local/lib/<short_name>/`) and its CLI
  (`/usr/local/sbin/<short_name>`),
- its config (`/etc/<short_name>/`: `<short_name>.conf`, `apps.conf`,
  `restic-sets.conf`, `hooks.conf`, all 0600) and the restic credentials
  (`offsite.env`, 0600),
- `/etc/sanoid/sanoid.conf` for tier 1,
- one systemd service + timer per enabled tier.

Every tier is toggled in the vault. Every name a customer sees (CLI banner and
help, alerts, reports, unit names and descriptions, install paths, restic
tags) comes from a brand block, so an MSP can ship it under its own name
(see "White-labelling").

The engine is the storage-host setup proven in production on a Proxmox NAS
(single USB SSD data pool, NVMe backup pool, restic to Backblaze B2), lifted
with its names and behaviour kept, de-branded, and extended with dump hooks,
the notifier and reports.

`<short_name>` below is `skbackup` unless the brand says otherwise.

## Why it exists, and what replaced what

The first skbackup (v2.19.0 to v2.22.0) was a Duplicati container on the
swarm, reading `/var/data` over NFS. It was never deployed on any instance.
Its model was wrong for the failure it was meant to cover: it ran on the same
cluster it protected, read live files (torn copies of anything mid-write), and
pushed every read of the data through NFS while prod was using it. This app
replaces it (see "Upgrading from the Duplicati skbackup").

The design comes from an estate where one physical box held both the
single-disk NFS pool and every swarm VM. Everything below is ordered by what
survives what:

| Tier | What | Survives | Does not survive |
|---|---|---|---|
| 1 | sanoid snapshots of the whole data dataset, plus a pre-deploy snapshot helper | deletes, bad deploys, fat fingers, ransomware on NFS clients (they cannot destroy snapshots) | loss of the data disk or the box |
| 2 | per-app copy from a frozen snapshot into a second pool on ANOTHER disk, daily/weekly/monthly history | loss or corruption of the data disk, a bad rollback | loss of the box |
| 3 | DB dump hooks and dump freshness checks, before the snapshots | makes tiers 1, 2 and 4 hold a consistent, portable dump, not only crash-consistent engine files | |
| 4 | restic (client-side encrypted, deduplicated) to S3/B2 or any restic backend | loss of the box, the house, the site | loss of the repository password |
| 5 | monthly restore test: one app plus random sample files restored from the OFFSITE copy, sha256 against the exact snapshot they came from | a backup that exists but cannot be restored | |
| 6 | staleness and health checks with alerts | silent failure of any tier | |

## Architecture

```
               NFS clients (swarm nodes) read and write /var/data
                                   |
 storage host  +-------------------+---------------------------------------+
               | data dataset tank/data <-- sanoid: autosnap_* hourly..monthly | tier 1
               |                           <-- predeploy: pre-<purpose>-<ts>   |
   05:30 sync  | [3] hooks.conf: dumps land INSIDE the dataset              |
               | [3] dump=DIR freshness check (warn, never blocks)          |
               |     zfs snapshot tank/data@<short>-<ts>   (frozen, all apps)|
               | [2] rsync .zfs/snapshot/<ts>/<src>/ -> backup/copies/<app> |  tier 2
               |     (local, --bwlimit 10000, ionice idle; newest=DIR:N)    |
               |     zfs snapshot backup/copies/<app>@daily-<ts> ...        |
               |     destroy the frozen snapshot                            |
 06:30 restic  | [4] mount the NEWEST autosnap_* read-only at /run/<short>/src |  tier 4
               |     restic backup per set: "apps" (every copied app, same  |
               |     excludes) and "paths" sets (big data, offsite only)    |
               +------------------------------------------------------------+
                                   |  --limit-upload
                                   v
                     S3 / B2 bucket (private, encrypted by restic)
```

- **Frozen source.** Copies and uploads never read the live tree. The copy
  reads a snapshot taken after the dump hooks. Restic reads the newest sanoid
  snapshot in single-dataset mode, or creates one atomic capture for a tree.
  Every app is copied as of one instant.
- **Stable snapshot path for restic.** The snapshot is mounted read-only at
  the same path every night (`offsite.mount_path`, default
  `/run/<short_name>/src`). restic picks its parent snapshot by path; with a
  path that changes every night (`.zfs/snapshot/<name>`) it would re-read
  every file every night.
- **Runs locally on the storage host.** No NFS hop, no load on the swarm.
  The per-app copy is capped (`copy.bwlimit_kbps`) and runs under
  `ionice -c3 nice -n19` in units with `IOSchedulingClass=idle`. ZFS does not
  honour ionice; the cap and the night slot are what protect prod (see
  "Lessons learned").
- **Sets.** `kind: apps` backs up every copied app with the same excludes
  and `newest=` rules as the copy (small, the important set). `kind: paths`
  backs up anything else, typically data too big for the copy pool (photo
  originals, a user's cloud drive). Sets run in list order: put small ones
  first so a multi-night first seed finishes them first.
- **Tags and host.** Each restic snapshot carries the set's name and
  `<short_name>`; `--host` is `host_label` (default: the short hostname).
  `forget` groups by host and tags.
- **Locks.** `sync` and the restic commands each hold a lock under
  `/run/lock/<short_name>-*.lock`; `predeploy` and `check` take none.

## Deploy

1. Put the storage host in an inventory group (default `storage_hosts`,
   override with `-e skbackup_hosts=<group>`). Give it the same `domain` and
   `cluster_name` host vars as the swarm managers so the vault resolves to
   `optional/group_vars/<env>/skbackup-<env>-<domain-dashed>-<cluster>_vault.yml`
   (without them the legacy `skbackup-<env>_vault.yml` is used). The play
   never touches the swarm: never limit it to a manager group.
2. Create the copy pool if tier 2 is on (the playbook never creates pools or
   formats disks), on a DIFFERENT disk from the data pool, for example:
   `zpool create -o ashift=12 -o autotrim=on -O compression=zstd -O atime=off -O xattr=sa -O acltype=posixacl -m /backup backup /dev/<disk>`
   and `zfs create backup/copies`. Do not add it to Proxmox's storage.cfg
   (keeps VM disks off it). The engine creates one child dataset per app.
3. Write the vault (below). Keep an offline copy of `offsite.password`.
4. Deploy:

   ```bash
   ansible-playbook -i <inventory> v1/ansible/optional/skbackup/deploy_skbackup-prod.yml \
     --vault-password-file <file> -e skbackup_hosts=<storage group>
   ```

   The play writes `/etc/sanoid/sanoid.conf` FIRST and only then installs
   the packages (`rsync util-linux python3`, `sanoid`, `restic` from apt or
   a pinned release): the sanoid package enables and starts its timer on
   install, and must find the policy already there. It then installs the
   engine, config files and timers, removes the timers of tiers that were
   switched off, creates the restic repository if it does not exist
   (`<short_name> restic init` is idempotent), and prints `<short_name> status`.
5. Prove it before trusting it: `<short_name> sync --app <small app>`, compare
   file count, bytes and a sha256 sample with the source snapshot; then
   `<short_name> restic backup` a small set, `restic check`, run it again
   (the second run must report a parent and unmodified files), and restore
   one file by hand.

The first full copy and the first offsite seed read everything once. Run them
at night, capped, and if your estate serialises heavy I/O on the data pool,
under that lock. A first seed of hundreds of GB spans nights: the offsite
unit stops after 22h and the next night resumes where it left off (dedup).

## Vault contract

`skbackup.*` in the instance vault, merged recursively over
`vars/defaults.yml`, so an instance sets only what differs. Lists replace.

Required:

| Key | Notes |
|---|---|
| `skbackup.data_dataset` | The ZFS dataset the swarm's NFS data lives on (`pool/path`, not a mount path). |

Top level:

| Key | Default | Notes |
|---|---|---|
| `skbackup.branding` | SKBackup brand | See "White-labelling". |
| `skbackup.unit_prefix` | `""` | Prefix for systemd unit NAMES: `acme-` gives `acme-<short_name>-sync.timer`. |
| `skbackup.host_label` | short hostname | restic `--host` and the host named in alerts and reports. |
| `skbackup.datasets` | single dataset | Recursive discovery, subtree exclusions, explicit private opt-ins and per-dataset retention. See "Dataset trees and restic tuning". |
| `skbackup.state_dir` | `/var/lib/<short_name>` | Stamps (`last-ok-<app>`, `restic-last-ok-<set>`), rsync logs, reports, `alerts.log`. |
| `skbackup.schedule` | see defaults | systemd `OnCalendar` for `sync` (05:30), `offsite` (06:30), `prune` (Sunday 13:00), `restore_test` (1st of the month 14:00), `check` (hourly at :17). |

Tier 1, `skbackup.snapshots`:

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | Installs sanoid and manages `/etc/sanoid/sanoid.conf` (refuses to overwrite one it did not write). |
| `retention` | 36 hourly, 30 daily, 8 weekly, 6 monthly | sanoid template (`hourly`, `daily`, `weekly`, `monthly`, `yearly`). |
| `predeploy_keep_days` / `predeploy_keep_min` | `30` / `2` | `predeploy` prunes `pre-*` snapshots older than this, always keeping the newest N. |

Tier 2, `skbackup.copy`:

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | |
| `target` | | Parent dataset on the backup pool (one child per app). Required when enabled. |
| `bwlimit_kbps` | `10000` | rsync `--bwlimit` in KiB/s. Measured: 40000 on a USB SSD pool pushed NFS writes from a node to 45 s; 10000 kept them under 1.4 s. Raise only with your own measurement. |
| `retention` | 7 daily, 4 weekly, 3 monthly | History snapshots per app; weekly on Sundays, monthly on the 1st. |
| `apps` | `[]` | `[{name, src, retention, excludes, newest, dump}]`: `name` names the copy dataset; `src` (default `name`) is the path under the data dataset; `retention` overrides per app (`{daily: 14}`); `excludes` are rsync patterns (`/x/` anchored at the app root, no whitespace); `newest: [{dir, keep}]` keeps only the newest N files of a dump dir; `dump: DIR` flags the app when DIR has no non-empty file newer than `dumps.max_age_hours`. |

Tier 3, `skbackup.dumps`:

| Key | Default | Notes |
|---|---|---|
| `max_age_hours` | `26` | Freshness limit for every app's `dump` dir. A stale dump is a WARN, never a blocker: one stale sidecar must not stop every other app from being copied. |
| `hooks` | `[]` | `[{name, command, timeout: 1800}]`, run in order by `sync` before it freezes the data (`bash -c`, operator-trusted like a cron line), for example a `docker exec ... pg_dump` on a manager over ssh. A failed hook is recorded and reported by `check`; the copy still runs. |

Tier 4, `skbackup.offsite`:

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | |
| `repository` | | Any restic repository: `s3:https://s3.<region>.backblazeb2.com/<bucket>`, `b2:<bucket>:<path>`, `rest:https://...`, a local path. Required. |
| `password` | | The restic repository password, written to `offsite.env` (0600). Required. **Losing it makes the repository unreadable: keep an offline copy.** |
| `env` | `{}` | Backend credentials. For the `s3:` backend, `B2_ACCOUNT_ID` / `B2_ACCOUNT_KEY` are mapped to `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (and `B2_REGION` to `AWS_DEFAULT_REGION`) unless the `AWS_*` names are set; restic's S3 backend reads only the `AWS_*` names. |
| `limit_upload_kbps` | `8000` | restic `--limit-upload` (KiB/s). Leave half the uplink to everything else. |
| `mount_path` | `/run/<short_name>/src` | The stable read-only mount of the snapshot restic reads. Paths inside the repository start with it. |
| `sets` | `[]` | `[{name, kind: apps}]` and/or `[{name, kind: paths, paths: [rel, ...], excludes: [...]}]`. `kind: apps` needs the copy tier. Path excludes starting with `/` are anchored at the snapshot root (`/app/data/tmp`). |
| `forget` | 7 daily, 4 weekly, 6 monthly | `restic forget --prune`, weekly, grouped by host and tags. |
| `seed_grace_days` | `7` | A set whose first upload started less than this ago reports PENDING, not STALE. |
| `install` | `apt` | `apt`, `url` (a pinned upstream release: set `url` and `sha256` of the `.bz2`) or `none`. Debian bookworm's restic is 0.14; prefer a pinned 0.18.x release. |
| `url` / `sha256` | | For `install: url`: the `restic_<ver>_linux_<arch>.bz2` asset and its sha256 from the release's `SHA256SUMS`. |

Tier 5, `skbackup.restore_test`:

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | Needs the offsite tier. |
| `app` | | The `src` of one copied app, restored whole from the `apps` set. |
| `sample_set` | | A `paths` set to draw random files from (large sets are sampled, not restored). |
| `samples` | `10` | Sample size. |

Tier 6, `skbackup.alerts`:

| Key | Default | Notes |
|---|---|---|
| `notifier` | `log` | `none` (no check timer: an alert host runs `check` over ssh, see below), `log` (the local check timer writes `<state_dir>/alerts.log`) or `command`. |
| `command` | | One line, split on whitespace, never run by a shell. Placeholders per word: `{level}` (info, warn, crit), `{key}`, `{subject}`, `{message}` (`[<alert_prefix>] <level>: <subject>`), `{body}`. The command also gets `BK_LEVEL`, `BK_KEY`, `BK_SUBJECT`, `BK_BODY`, `BK_BRAND_PRODUCT`, `BK_BRAND_LOGO_URL`, `BK_BRAND_CONTACT`. |
| `check_repo` | `false` | Also ask the repository for the newest snapshot of each set. Catches a repository that lost snapshots or became unreachable, at the cost of B2 class C calls every hour. |
| `max_age` | snapshot 120 min, copy 26 h, offsite 36 h | Staleness thresholds. |
| `pool_capacity_pct` | `80` | Warn when the data or backup pool is this full (or not ONLINE). |
| `thinpool` / `thinpool_pct` | `""` / `85` | An LVM thin pool under the backup pool (for example `pve/data`): if it fills, every VM on it pauses. |

The deploy fails closed on: an empty or path-shaped `data_dataset`; copy on
without a target or apps, an app name or path that is not safe, an exclude
with whitespace; a hook without a name or command; offsite on without a
repository, password or sets, a set with an unknown kind, an `apps` set
without the copy tier, a `paths` set without paths; a restore test naming an
app that is not copied or a set that is not a `paths` set; `install: url`
without a 64-character sha256; a `command` notifier without a command; a
short name or unit prefix that is not a safe path/unit component; and any key
of the retired Duplicati skbackup.

## Tiers in practice

### Tier 1: snapshots and the pre-deploy helper

sanoid snapshots the data dataset (`autosnap_*`) and prunes its own
snapshots only. Space used is churn: a daily-dump-heavy estate might pin 5
to 15 GB a day. Snapshots also pin DELETED data: decide what to do with
legacy data BEFORE retention starts, or the space only comes back when the
last monthly snapshot that holds it expires.

Before any deploy, take a snapshot instead of copying data:

```bash
ssh <storage-host> <short_name> predeploy skgit-upgrade
# tank/data@pre-skgit-upgrade-20270115T080000Z
ssh <storage-host> <short_name> predeploy --list
```

Instant and zero I/O. Plus a small DB dump OFF the storage host when the
service has a database. It prunes `pre-*` snapshots older than
`predeploy_keep_days` (keeping the newest `predeploy_keep_min`). For a
one-off delayed destroy (a safety snapshot to drop in 7 days), use a
systemd one-shot timer (`OnCalendar=<date>`, `ExecStart=zfs destroy
<exactly one snapshot>`, `ExecStartPost=systemctl disable <timer>`), not
`at`: `at` is often not installed and leaves no unit to inspect.

### Tier 2: per-app copy

Pick apps by what they hold, not by size (see "Choosing apps"). The copy pool
sits on a different disk. If it is a thin LV on the hypervisor's own NVMe,
set `alerts.thinpool`: if the thin pool fills, every VM on it pauses.

### Tier 3: dumps

The snapshots are atomic and the NFS export is `sync`, so raw database files
in them are crash-consistent (they restore like a power loss). Dumps are
what is portable and testable. Either let each stack's dump sidecar write
into its app directory and flag staleness with `dump: DIR`, or produce the
dump in a `hook`. Schedule `sync` after the sidecars, and keep the newest 2
dumps (`newest: [{dir, keep: 2}]`) of any sidecar that might still be
writing when the snapshot is taken, so the copy always holds one complete
dump. Watch the sidecars' own schedules: a sidecar whose schedule drifts
(one measured drifting about 13 minutes a day) will eventually cross the
sync slot; pin its schedule to a fixed time. And watch for sidecars that
keep a single generation (overwrite one file forever): a failed write then
truncates the only copy.

### Tier 4: offsite

`<short_name> restic backup` mounts frozen sources at the stable path and
backs up every set. Single-dataset mode uses the newest `autosnap_*`; tree
mode captures all included filesystems atomically with one common name. The first seed of a large set takes
nights at a sane upload limit; the daily increment is then small. gzip
defeats restic dedup; dump with `gzip --rsyncable` (or uncompressed) where
you can. `<short_name> restic forget-prune` runs weekly (B2 class B/C
calls cost money). `<short_name> restic check 5%` reads a data subset (B2
download cost).

### Tier 5: restore test

Monthly: restores `restore_test.app` whole from the `apps` set and
`samples` random files from `sample_set`, and compares each file's sha256
with the SAME file in the ZFS snapshot it was backed up from (or, if that
snapshot has expired, the live file when size and mtime still match;
otherwise it is counted as skipped). Any mismatch, or nothing compared, is a
failure. The report lands in `<state_dir>/reports/restore-test-<ts>.txt` and
is sent as `info` (pass) or `crit` (fail).

### Tier 6: checks and alerts

`<short_name> check` prints one line per check (`OK`, `STALE`, `WARN`,
`PENDING`) and exits 1 if anything is STALE or WARN: the newest sanoid
snapshot, the newest `daily-*` copy per app, dump freshness and failed
hooks at the last sync, the last good restic run per set (state files;
`check_repo` also asks the repository), pool health and capacity, the thin
pool. The report is in `<state_dir>/reports/check-latest.txt`.

Two ways to alert:

- **On the storage host**: the check timer runs `check --notify`, one alert
  per finding through the notifier (`log` or `command`), keyed for
  de-duplication.
- **From an alerting host** (where your alert CLI lives, with key-based ssh
  to the storage host): set `alerts.notifier: none` and schedule
  `/usr/local/lib/<short_name>/alert-host-check` there (copy it over):

  ```
  17 * * * * /opt/backup/alert-host-check <storage-host> /usr/local/sbin/<short_name> <alert_prefix> /opt/alerts/bin/send-alert -l {level} -k {key} {message}
  ```

  It pages on any STALE/WARN line and when the storage host cannot be
  reached over ssh. The alert host needs ssh access to the storage host
  (BatchMode, a dedicated key; the command it runs is read-only).

Use absolute paths in anything a scheduler runs: cron and systemd have no
login PATH.

## CLI

```
<short_name> sync [--app NAME]... [--dry-run]   tier 2 (+ dump hooks)
<short_name> restic init                        create the repository (idempotent)
<short_name> restic backup [SET]...             tier 4
<short_name> restic forget-prune                retention + prune
<short_name> restic check [SUBSET]              repository check (SUBSET like 5% reads data)
<short_name> restic restore-test                tier 5
<short_name> restic snapshots                   newest snapshot per set
<short_name> check [--notify]                   tier 6
<short_name> predeploy PURPOSE | --list         pre-deploy snapshots
<short_name> status                             what is configured and when each tier last succeeded
```

```bash
systemctl list-timers '<prefix><short_name>-*' sanoid.timer
journalctl -u '<prefix><short_name>-sync' -u '<prefix><short_name>-offsite' --since today
```

## Restore procedures

Restore BESIDE the live data or with the app's stack stopped; a restore into
the data dataset is a heavy NFS write, so throttle it.

**Tier 1 (a file or an app from a data snapshot)**, on the storage host as
root (`.zfs` is hidden but reachable by path):

```bash
zfs list -t snapshot -o name,creation tank/data | tail
cp -a /tank/data/.zfs/snapshot/<snap>/<app>/<path> /tank/data/<app>/<path>
```

For a big restore, `zfs clone tank/data@<snap> tank/restore-tmp` is instant;
copy from there. **Never `zfs rollback` the data dataset**: it rolls back
every app.

**Tier 2 (the data disk is gone or corrupt)**, with the app's stack stopped:

```bash
zfs list -t snapshot -r backup/copies/<app>          # daily-/weekly-/monthly-<ts>
ionice -c3 rsync -aHAX --numeric-ids --bwlimit=10000 \
  /backup/copies/<app>/.zfs/snapshot/daily-<ts>/ /tank/data/<src>/
```

No `--delete` unless you mean it; excluded paths (previews, caches) are not
in the copy and are rebuilt by the app.

**Tier 3 (a database)**: restore the app's dump directory with tier 1, 2 or
4, then load the dump with that service's own restore procedure
(`pg_restore`, `mysql <`). Raw database files from a snapshot are a fallback
(crash recovery on start).

**Tier 4 (the box is gone)**, on any machine with restic and the
credentials:

```bash
set -a; . ./offsite.env; set +a            # RESTIC_REPOSITORY, RESTIC_PASSWORD, B2_*
export AWS_ACCESS_KEY_ID=$B2_ACCOUNT_ID AWS_SECRET_ACCESS_KEY=$B2_ACCOUNT_KEY   # s3: backend
restic snapshots --latest 1 --group-by host,tags
restic restore latest --tag <apps set> --include /run/<short_name>/src/<app> --target /var/tmp/restore
restic restore latest --tag <paths set> --target /mnt/<big disk>/restore
```

Paths inside the repository start with `mount_path` (default
`/run/<short_name>/src/`).

**Tier 5** is the monthly rehearsal of tier 4: read its report.

## Choosing apps

- **Copy and offsite:** app config and secrets (rendered env, keys, TLS state
  such as an ACME store: losing it can mean nothing re-obtains a wildcard
  certificate), the newest DB dumps, mail, git repositories and LFS, user
  files, anything a person made.
- **Offsite only (`kind: paths`):** irreplaceable data too big for the copy
  pool: photo originals, a user's cloud drive. If there is NO other copy, it
  must be offsite.
- **Skip:** anything regenerable: thumbnails, previews, transcoded video,
  model caches, package registries a CI re-publishes, runner caches, search
  indexes, repo archives, trash and old file versions you would not restore,
  logs and metrics.
- **Raw database directories:** skip in tiers 2 and 4 when a dump exists
  (tier 1 still holds them crash-consistent). A database on a node-local
  volume is NOT in the data snapshots at all: only its dump is.
- **Config directories contain secrets:** the copy pool must be root-only and
  offsite must be encrypted (restic is).

## B2 setup (Backblaze, S3 API)

1. **Private bucket**, default encryption (SSE-B2) on. restic encrypts
   client-side anyway; SSE is defence in depth.
2. **Lifecycle: keep only the last version** of each file. restic deletes
   objects on prune; with the default "keep all versions", deleted objects
   stay billable forever. (A longer "days before hiding/deleting" gives an
   undo window against a compromised key, at a storage cost.)
3. **Application key scoped to that one bucket**, read and write. Never use
   the master key on a host. Put the key ID and key in `offsite.env` as
   `B2_ACCOUNT_ID` / `B2_ACCOUNT_KEY` (the engine maps them to `AWS_*` for
   the S3 backend) and use the bucket's S3 endpoint:
   `repository: s3:https://s3.<region>.backblazeb2.com/<bucket>`.
4. **Raise the caps before the first seed.** A new account has daily caps on
   storage, download bandwidth AND transactions (class B: downloads/gets,
   class C: list and other calls). restic's `check`, `prune` and
   `snapshots` are transaction-heavy; hitting a cap makes the backend return
   errors mid-run. Set caps above expected use (or remove them) and turn on
   the cap alerts.
5. Store the repository password and the key in your secret store with an
   offline copy. Test a restore from a machine that is not the storage host.

Any S3-compatible target (a self-hosted object store on ANOTHER site, Wasabi,
S3) works the same way; a same-site object store protects against deletion,
not against losing the site.

## White-labelling

`skbackup.branding` follows the framework's vanity-layer pattern
(`v2/skwire/branding.py`): a name, a tagline, a logo, optional credits.

| Key | Default | Where it shows |
|---|---|---|
| `product_name` | `SKBackup` | CLI banner, help, status, reports, unit `Description=`, config headers. |
| `short_name` | `skbackup` | The command, `/usr/local/lib/<short_name>`, `/etc/<short_name>`, `/var/lib/<short_name>`, `/run/<short_name>/src` (restic paths), unit names, lock files, alert keys, the frozen snapshot names, a restic tag. `^[a-z][a-z0-9-]{1,30}$`. |
| `tagline` | the SKStacks tagline | Banner and help. |
| `alert_prefix` | `SKBackup` | Every alert subject: `[<alert_prefix>] crit: ...`. |
| `report_footer` | `SKBackup, part of SKStacks` | Footer of every alert body, report and the help and status text. |
| `contact` | `""` | `Contact:` line under the footer. |
| `logo_url` | `""` | Report header, and `BK_BRAND_LOGO_URL` for notifier commands (webhooks, e-mail). |
| `credits` / `credits_text` | `false` / Powered by SKStacks | When `credits: true`, the credits line is added to the banner, footer and reports. |

Plus `unit_prefix` (unit names) and `host_label` (restic `--host`).

**Rebrand for a customer:**

```yaml
skbackup:
  branding:
    product_name: Acme Vault
    short_name: acmevault
    tagline: Your data, kept safe by Acme IT
    alert_prefix: ACME-VAULT
    report_footer: Acme IT managed backup service
    contact: support@acme.example
  unit_prefix: acme-
```

The host then has `/usr/local/sbin/acmevault`, `acme-acmevault-sync.timer`
("Acme Vault: dump hooks and per-app copy ..."), alerts `[ACME-VAULT] crit:
...` and reports signed by Acme IT. The engine carries no brand string of its
own (it prints only what the config says), and the test suite renders the
deploy in white-label mode and fails if any default brand string appears in
any rendered file, install path, shell body, or anything the engine prints,
alerts, reports or names on disk (`v1/tests/test_skbackup_whitelabel.py`).
Changing `short_name` on an existing host installs under the new name:
disable the old units (`systemctl disable --now <old>-*.timer`) and remove
the old paths by hand; restic paths change with it, so the next upload of
each set re-reads everything once.

## Upgrading from the Duplicati skbackup

The Duplicati app (v2.19.0 to v2.22.0) was never deployed on an instance, so
there is nothing to migrate. On instances whose vault still carries its keys
(`settings_encryption_key`, `ui_password`, `passphrase`, `jobs`, `exclude`,
`DUPLICATI_VERSION`, `source_path`, `source_read_only`, `CLI_ARGS`), remove
them: the deploy refuses them rather than ignore them. `CLUSTERNAME`,
`DOMAIN`, `ACME_ENABLED` and `networks` are simply unused now. If an old
private Duplicati copy left state on NFS (`/var/data/skbackup-<env>`,
`/var/data/config/skbackup-<env>`) and no job ever wrote to a target from
it, delete it once a tier 1 snapshot holds it. No overlay network or swarm
stack is needed.

## Lessons learned

- **The bandwidth cap is the prod safety, and 40 MB/s was too much.** On a
  single USB SSD data pool, a copy at 40000 KiB/s pushed an NFS write from a
  swarm node to 45 s; at 10000 KiB/s the worst probe was 1.4 s with every
  service healthy. The default is 10000; measure before raising it (time an
  NFS `touch`+`rm` from a node while the copy runs).
- **A copy on the same disk is not a backup.** Bulk copies into a backups
  directory on the NFS pool doubled the space and protected against nothing
  that loses the disk. A snapshot gives the same point-in-time safety
  instantly, at zero I/O. Heavy I/O on the shared pool is a prod change: an
  unthrottled bulk copy of one app's data once outran another service's
  health check and caused a long outage.
- **restic needs a stable path.** Backing up the snapshot from its
  `.zfs/snapshot/<name>` path, which changes every night, makes restic find
  no parent and re-read everything. Mount the snapshot at the same path.
- **S3 means `AWS_*`.** restic's S3 backend reads `AWS_ACCESS_KEY_ID` and
  `AWS_SECRET_ACCESS_KEY` only; B2 key names in the env file do nothing for
  an `s3:` repository unless mapped.
- **Config before package.** The sanoid package enables and starts its timer
  on install; write `/etc/sanoid/sanoid.conf` first (and create
  `/etc/sanoid`: Ubuntu's package ships none).
- **No `at`.** Delayed one-off actions (dropping a safety snapshot in 7 days)
  are systemd one-shot timers naming exactly one target.
- **The alert host needs a way in.** An hourly check driven from an alerting
  host needs key-based ssh to the storage host; test the ssh failure path (it
  must page too).
- **Dumps drift.** A dump sidecar's schedule can drift (about 13 min a day
  measured) until it crosses the sync slot. Pin sidecar schedules, keep the
  newest 2 dumps, and let the dump check flag staleness.
- **One box can be all of prod.** When the NFS pool and every swarm VM share
  a host, only an off-box copy survives that host. A single-disk pool can
  detect corruption but not repair it: a mirror is not a backup, but it
  removes the largest single risk cheaply.
- **Snapshots pin deleted data.** Decide on legacy data before retention
  starts.
- **"Succeeded" is not "restorable".** The restore test reads the offsite
  copy and compares it with the exact snapshot it came from.
- **Keep the password offline.** A restic repository without its password is
  noise.

## Testing

- `v1/tests/test_skbackup_render.py`: the real deploy playbook through
  Ansible (default and white-label), the records the engine reads, unit
  ordering, fail-closed validation.
- `v1/tests/test_skbackup_whitelabel.py`: the brand-leak scan.
- `v1/tests/test_skbackup_engine.py`: the engine on a scratch tree, with
  zfs/zpool/mount as test doubles (`v1/tests/fakezfs.py`, via
  `BACKUP_TEST_PATH`) and a real local restic repository: copy, excludes,
  newest-N, hooks, locking, pre-deploy pruning, backup and byte-exact
  restore, parent reuse, restore test pass and mismatch, repository checks,
  staleness, notifier.
- `v1/tests/test_skbackup_shellcheck.py`: `bash -n` and shellcheck.
- The render gate renders `vars/skbackup.example.yml` (default brand) and
  `vars/skbackup.set.yml` (every tier, white-label brand).
- skstack06 has a `skbackup` stage that deploys it on a test node with
  loopback ZFS pools and a local restic repository.


### Dataset trees and restic tuning

`skbackup.datasets` defaults to one dataset: `{recursive: false, exclude: [], include_private: [], retention: {}}`.
Set `recursive: true` to discover the filesystem tree below `data_dataset`. Exclusions are complete dataset names below that root and cover descendants. A `*-private` or `recovery*` component fails closed unless excluded or the exact dataset is in `include_private`. The deploy checks privacy before enabling sanoid; every engine operation repeats discovery. Per-dataset `retention` maps override sanoid hourly/daily/weekly/monthly counts. When hourly snapshots are disabled, freshness checks allow one daily/weekly/monthly interval plus the configured grace instead of raising a false hourly alarm. The existing single-file sanoid ownership guard still applies.

For example:

```yaml
skbackup:
  data_dataset: tank/data
  datasets:
    recursive: true
    exclude: [tank/data/backups, tank/data/recovery-old]
    retention:
      tank/data/share/cloud: {hourly: 0, daily: 14}
  copy:
    apps: [{name: app-one, src: shared/app-one}]
  offsite:
    sets:
      - {name: users, kind: paths, paths: [share/users]}
      - {name: cloud, kind: paths, paths: [share/cloud], limit_upload_kbps: 4000, pack_size_mib: 64}
```

For a recursive tree, restic creates one atomic capture with a unique `<short_name>-restic-<uuid>` name on every included filesystem. An unrestricted filesystem tree uses `zfs snapshot -r`; if excluded descendants or volumes exist, one `zfs snapshot` command names only the included filesystems. This keeps excluded/private datasets and zvols untouched. A parent snapshot alone contains empty child mount directories, so every included child is mounted separately. Single-dataset mode continues to use the newest sanoid snapshot. The mount tree starts with a private tmpfs at the stable source root and prepares each included dataset-relative directory there, so custom live mountpoints outside their parents do not change repository paths. Frozen parents are inspected at temporary read-only mounts outside the backup tree. Empty container parents leave the prepared directories visible; file-holding parents mount at their own relative paths before their children. Those parents must already contain the child mount directories without symlinks; missing or unsafe directories fail closed instead of writing into a read-only snapshot. Every snapshot mount, inspection mount and the tmpfs is unwound in reverse by the exit trap on success, partial failure or termination. Failed unmounts retain the frozen sources. Single-dataset mode keeps its existing direct snapshot mount. Paths are relative to the root; excluded descendants are also filtered when a set covers their ancestor. Tier-2 apps must fit within one included dataset; only that owning dataset is frozen. Symlink source paths are refused. Restore tests record every source snapshot and compare against the originating dataset, with the existing unchanged-live-file fallback after snapshot expiry. The engine records capture ownership before the atomic operation, retains captures referenced by each set's last successful backup, and releases unreferenced owned captures after unmounting. A selected-set run preserves other sets' references; a failed run preserves prior successful references. External snapshots, including matching name prefixes, are never claimed or pruned. Failed unmounts retain frozen sources and return failure. Restore tests hold the same backup lock so their source snapshots cannot be released while comparison is running. These offsite captures are separate from sanoid tier-1 retention; removing a set also requires the operator to retire its provenance file before its last capture can be released.

Vault options under `offsite`: `pack_size_mib: 16` (per-set override supported), `compression: auto` (`max` or `off` also accepted), `read_concurrency` (unset uses restic's default), `retry_lock: 30m`, `exclude_caches: true`, `exclude_if_present: [.nobackup]`, `prune_max_unused: 5%`, `check_subset: 2.5%`. Each set can override `limit_upload_kbps`. A weekly subset-check timer is installed for recursive trees or an explicitly configured `check_subset`; `schedule.repo_check` defaults to Sunday 15:00. Older single-dataset vaults keep identical rendered configuration and units. `restic check [SUBSET]` uses the configured subset by default.

`alerts.healthchecks_url` optionally pings on command success or failure alongside existing log/command alerts. The URL lives in a separate 0600 credentials file and is never logged. `snapshots.adopt_existing: false` leaves other tools' snapshots unmanaged; enabling it reports their external provenance during checks without pruning, renaming or using them as sanoid snapshots. Existing snapshot scheduling must be reconciled by the operator, not deleted by the engine.
