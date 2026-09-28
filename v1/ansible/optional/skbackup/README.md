# skbackup

Host-level backups for the storage host that serves a cluster's data, and the
framework's backup pattern doc. skbackup is **not a swarm stack**: it is an
Ansible playbook (`deploy_skbackup-<env>.yml`) that targets a storage-host
inventory group (a Proxmox node or any Linux host that exports the swarm's
`/var/data` over NFS, ideally from ZFS) and installs there:

- a small bash engine (`/usr/local/lib/<short_name>/`) and its CLI
  (`/usr/local/sbin/<short_name>`),
- ONE config file (`/etc/<short_name>/<short_name>.conf`, mode 0600),
- restic credentials (`/etc/<short_name>/offsite.env` and `offsite.pass`, 0600),
- `/etc/sanoid/sanoid.conf` when sanoid runs tier 1,
- one systemd service + timer per enabled tier.

Every tier is toggled in the vault. Every name a customer sees (CLI banner and
help, alerts, reports, unit names and descriptions, install paths, restic
tags) comes from a brand block, so an MSP can ship it under its own name
(see "White-labelling").

`<short_name>` below is `skbackup` unless the brand says otherwise.

## Why it exists, and what replaced what

The first skbackup (v2.19.0 to v2.22.0) was a Duplicati container on the
swarm, reading `/var/data` over NFS. It was never deployed on any instance.
Its model was wrong for the failure it was meant to cover: it ran on the same
cluster it protected, read live files (torn copies of anything mid-write), and
pushed every read of the data through NFS while prod was using it. This app
replaces it (see "Upgrading from the Duplicati skbackup").

The design comes from a real estate where one physical box held both the
single-disk NFS pool and every swarm VM. Everything below is ordered by what
survives what:

| Tier | What | Survives | Does not survive |
|---|---|---|---|
| 1 | ZFS snapshots of the whole data dataset (sanoid), plus a pre-deploy snapshot helper | deletes, bad deploys, fat fingers, ransomware on NFS clients (they cannot destroy snapshots) | loss of the data disk or the box |
| 2 | per-app copy from a frozen snapshot into a second pool on ANOTHER disk, with daily/weekly/monthly history | loss or corruption of the data disk, a bad rollback | loss of the box |
| 3 | DB dump hooks and dump freshness gates, before the snapshot | makes tiers 1, 2 and 4 hold a consistent, portable dump, not only crash-consistent engine files | |
| 4 | restic (client-side encrypted, deduplicated) to S3/B2 or any restic backend | loss of the box, the house, the site | loss of the repository password |
| 5 | monthly restore test: sampled restore from the OFFSITE copy vs the frozen source, plus `restic check` | a backup that exists but cannot be restored | |
| 6 | staleness and capacity checks with alerts | silent failure of any tier | |

## Architecture

```
            NFS clients (swarm nodes) read and write /var/data
                                  |
 storage host  +------------------+-------------------------------------+
               |  data dataset (tank/data)  <-- sanoid: hourly..monthly  |  tier 1
               |        |                        pre-<purpose>-<ts>      |
   05:30 run:  |  [3] dump hooks -> dumps land INSIDE the dataset        |
               |  [3] dump checks: newest dump per app fresh, non-empty  |
               |      (a failure aborts the run: no stale dump is saved) |
               |  zfs snapshot tank/data@<short>-run-<ts>  (frozen)      |
               |        |                                                |
               |  [2] rsync .zfs/snapshot/<run>/<app>/ -> backup/<app>   |  tier 2
               |      (local, throttled, no network hop, never torn)     |
               |      zfs snapshot backup/<app>@<short>-daily-<ts> ...   |
               |        |                                                |
               |  [4] cd <newest copy snapshot> && restic backup .       |  tier 4
               |      --parent <previous> --tag set:,app:,src:          |
               |  destroy the transient run snapshot                     |
               +---------------------------------------------------------+
                                  |  upload limit
                                  v
                    S3 / B2 bucket (private, encrypted by restic)
```

- **Frozen source.** Copies and uploads never read the live tree. They read a
  snapshot taken after the dumps, so every app is copied as of one instant.
- **Runs locally on the storage host.** No NFS hop, no load on the swarm. The
  per-app copy is capped (`copy.bwlimit_kbps`) and runs under
  `ionice -c3 nice -n19`. ZFS does not honour ionice; the cap, the 05:30 slot
  and the incremental rsync (metadata plus changed files after the first run)
  are what protect prod.
- **Offsite reads the copy, not the data disk**, for `source: copy` sets. A
  `source: data` set (for data too big for the copy pool, like photo
  originals) reads the run's frozen snapshot of the data dataset.
- **One restic snapshot per set and app**, taken inside the frozen directory
  (`restic backup .`), so the tree always starts at the app's root and the
  previous snapshot of the same set/app is passed as `--parent`. Tags:
  `<short_name>`, `offsite.tags`, the set's `tags`, `set:<set>`, `app:<app>`,
  `src:<frozen snapshot name>` (the restore test uses it to find the exact
  source). `--host` is `host_label`.
- **One lock** (`/run/<short_name>.lock`): run, copy, offsite, prune,
  restore-test and the builtin snapshot never overlap. `predeploy` does not
  take it (a snapshot is safe beside a running copy). `check` does not either.

### Snapshot backends

`snapshot_backend: zfs` (production): snapshots are ZFS snapshots (atomic, no
I/O), read through `<mountpoint>/.zfs/snapshot/<name>` (works with
`snapdir=hidden`). Copy targets are child datasets, created on demand under
`copy.target` (the pool itself must exist).

`snapshot_backend: dir` (hosts and test clusters without ZFS): a snapshot is
an `rsync --link-dest` tree under `snap_dir_root`. Unchanged files are hard
links to the PREVIOUS snapshot, never to the live tree, so a snapshot is frozen
against later in-place writes. The first snapshot of a target is a full copy,
so this is for small data and tests, not terabytes. Tier 1 then uses the
`builtin` engine (count-based retention), since sanoid is ZFS-only.

## Deploy

1. Put the storage host in an inventory group (default `storage_hosts`,
   override with `-e skbackup_hosts=<group>`). Give it the same `domain` and
   `cluster_name` host vars as the swarm managers so the vault resolves to
   `optional/group_vars/<env>/skbackup-<env>-<domain-dashed>-<cluster>_vault.yml`
   (without them the legacy `skbackup-<env>_vault.yml` is used).
2. Create the copy pool if tier 2 is on (the playbook never creates pools or
   formats disks). For example, on a second disk:
   `zpool create -o ashift=12 -o autotrim=on -O compression=zstd -O atime=off -O xattr=sa -O acltype=posixacl -m /backup backup /dev/<disk>`.
   Do not add it to Proxmox's storage.cfg (keeps VM disks off it).
3. Write the vault (below). Keep an offline copy of `offsite.password`.
4. Deploy:

   ```bash
   ansible-playbook -i <inventory> v1/ansible/optional/skbackup/deploy_skbackup-prod.yml \
     --vault-password-file <file>
   ```

   The play installs packages (`rsync jq util-linux`, `sanoid` when tier 1
   uses it, `restic` from apt or a pinned release), the engine, config, units
   and timers, removes the timers of tiers that were switched off, creates the
   restic repository if it does not exist, and prints `<short_name> status`.
5. Prove it before scheduling trust: `<short_name> run` once by hand for a
   small app (`copy.apps` with one entry), then `restore copy` and
   `restore offsite` it beside the original and compare sha256 sums.

The first full copy and the first offsite seed read everything once. Run them
at night, capped, and if your estate serialises heavy I/O on the data pool,
under that lock.

## Vault contract

`skbackup.*` in the instance vault, merged recursively over
`vars/defaults.yml`, so an instance sets only what differs. Lists replace.

Required:

| Key | Notes |
|---|---|
| `skbackup.data_target` | The data: a dataset (`tank/data`) with `zfs`, an absolute directory with `dir`. |

Top level:

| Key | Default | Notes |
|---|---|---|
| `skbackup.branding` | SKBackup brand | See "White-labelling". |
| `skbackup.unit_prefix` | `""` | Prefix for systemd unit NAMES: `acme-` gives `acme-<short_name>-run.timer`. |
| `skbackup.host_label` | inventory hostname | restic `--host` and the host shown in alerts and reports. |
| `skbackup.snapshot_backend` | `zfs` | `zfs` or `dir`. |
| `skbackup.state_dir` | `/var/lib/<short_name>` | Stamps, reports, `alerts.log`, `dir` snapshots. |
| `skbackup.snap_dir_root` | `<state_dir>/snapshots` | `dir` backend only. |
| `skbackup.ionice` | `true` | Copies and uploads under `ionice -c3 nice -n19`. |
| `skbackup.schedule` | see defaults | systemd `OnCalendar` for `run` (05:30), `prune` (Sunday 07:00), `restore_test` (first Sunday 08:00), `check` (hourly at :17). |

Tier 1, `skbackup.snapshots`:

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | |
| `engine` | `sanoid` | `sanoid` (zfs only; manages `/etc/sanoid/sanoid.conf` and refuses to overwrite one it did not write) or `builtin`. |
| `retention` | 36 hourly, 30 daily, 8 weekly, 6 monthly | sanoid template (`hourly`, `daily`, `weekly`, `monthly`, `yearly`). |
| `builtin_keep` / `builtin_schedule` | `48` / `hourly` | builtin engine: keep the newest N `<short_name>-auto-<ts>` snapshots. |
| `predeploy_keep_days` / `predeploy_keep_min` | `30` / `2` | `predeploy` prunes `pre-*` snapshots older than this, always keeping the newest N. |

Tier 2, `skbackup.copy`:

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | |
| `target` | | Parent dataset (zfs) or directory (dir) on ANOTHER disk. Required when enabled. |
| `bwlimit_kbps` | `40000` | rsync `--bwlimit` (KiB/s); `0` = unlimited. |
| `retention` | 7 daily, 4 weekly, 3 monthly | History snapshots per app; weekly on Sundays, monthly on the 1st (UTC). |
| `apps` | `[]` | `[{name, excludes: [rsync patterns], retention: {daily: 14}}]`; `name` is a directory under the data target. |

Tier 3, `skbackup.dumps`:

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | |
| `required` | `true` | A failed hook or a failed check aborts the run (nothing is snapshotted, copied or uploaded). |
| `hooks` | `[]` | `[{name, command, timeout: 1800}]`, run in order with `bash -c` (operator-trusted, like a cron line), for example a `docker exec ... pg_dump` on a manager over ssh. |
| `checks` | `[]` | `[{app, glob, max_age_hours: 26}]`: the newest file matching `<data>/<app>/<glob>` must exist, be non-empty and fresh. |

Tier 4, `skbackup.offsite`:

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | |
| `repository` | | Any restic repository: `s3:https://s3.<region>.backblazeb2.com/<bucket>/<host>`, `b2:<bucket>:<path>`, `rest:https://...`, a local path. Required. |
| `password` | | The restic repository password. Required. **Losing it makes the repository unreadable: keep an offline copy.** |
| `env` | `{}` | Backend credentials: `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (S3 API), or `B2_ACCOUNT_ID` + `B2_ACCOUNT_KEY` (native b2). Written only to `offsite.env` (0600). |
| `limit_upload_kbps` | `8000` | restic `--limit-upload` (KiB/s). Leave half the uplink to everything else. |
| `tags` | `[]` | Extra tags on every snapshot; `<short_name>` is always added. |
| `ignore_inode` | `true` | `--ignore-inode`: the frozen path changes every night. |
| `sets` | `[]` | `[{name, source: copy or data, apps: [...], tags: [...], excludes: [...]}]`. A `copy` set may only name apps in `copy.apps`. |
| `forget` | 14 daily, 8 weekly, 12 monthly | `restic forget --keep-<k>` per set and app (`last`, `hourly`, `daily`, `weekly`, `monthly`, `yearly`), grouped by host only. |
| `install` | `apt` | `apt`, `url` (a pinned upstream release: set `url` and `sha256` of the `.bz2`) or `none`. Debian bookworm's restic is 0.14; prefer a pinned 0.18.x release. |
| `url` / `sha256` | | For `install: url`: the `restic_<ver>_linux_<arch>.bz2` asset and its sha256 from the release's `SHA256SUMS`. |

Tier 5, `skbackup.restore_test`:

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | Needs the offsite tier. |
| `samples` | `20` | Files per set/app streamed back with `restic dump` and compared by sha256 with the frozen source. |
| `read_data_subset` | `5%` | `restic check --read-data-subset`. |
| `hooks` | `[]` | `[{name, command, timeout}]`: extra checks, for example loading a restored dump into a throwaway database container. |

Tier 6, `skbackup.alerts`:

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | The check timer, and delivery through the notifier. `alerts.log` is always written. |
| `notifier` | `log` | `log` (only `<state_dir>/alerts.log`) or `command`. |
| `command` | | One line, split on whitespace, never run by a shell. Placeholders per word: `{level}` (info, warn, crit), `{key}`, `{subject}`, `{message}` (`[<alert_prefix>] <level>: <subject>`), `{body}`. The command also gets `BK_LEVEL`, `BK_KEY`, `BK_SUBJECT`, `BK_BODY`, `BK_BRAND_PRODUCT`, `BK_BRAND_LOGO_URL`, `BK_BRAND_CONTACT` in its environment. |
| `max_age_hours` | snapshot 2, copy 26, dumps 26, offsite 26, restore_test 840 | Staleness thresholds. |
| `pool_capacity_pct` | `80` | Warn when the data or copy pool is this full. |

The deploy fails closed on: an empty `data_target`, an unknown backend,
sanoid without zfs, copy on without a target or apps, offsite on without a
repository, password or sets, a `copy` set naming an app that is not copied,
`install: url` without a 64-character sha256, a `command` notifier without a
command, a short name or unit prefix that is not a safe path/unit component,
and any key of the retired Duplicati skbackup.

## Tiers in practice

### Tier 1: snapshots and the pre-deploy helper

sanoid snapshots the data dataset hourly and prunes it (`autosnap_*`). Space
used is churn: a daily-dump-heavy estate might pin 5 to 15 GB a day.
Snapshots also pin DELETED data: decide what to do with legacy data BEFORE
retention starts, or the space only comes back when the last monthly
snapshot that holds it expires.

Before any deploy, take a snapshot instead of copying data:

```bash
ssh <storage-host> <short_name> predeploy skgit-upgrade
# snapshot: tank/data@pre-skgit-upgrade-20270115T080000Z
```

It prunes `pre-*` snapshots older than `predeploy_keep_days` (keeping the
newest `predeploy_keep_min`); sanoid never touches them. Turn on
`autotrim=on` for SSD pools.

### Tier 2: per-app copy

Pick apps by what they hold, not by size (see "Choosing apps"). The copy pool
sits on a different disk. If it is a thin LV on the hypervisor's own NVMe,
keep an eye on the thin pool: if it fills, every VM on it pauses. Alert on
its `Data%` separately.

### Tier 3: dumps

The snapshot is atomic and the NFS export is `sync`, so raw database files in
it are crash-consistent (they restore like a power loss). Dumps are what is
portable and testable. Either let each stack's dump sidecar write into its app
directory and gate on freshness with `checks`, or produce the dump in a
`hook`. Schedule the run after the sidecars. Watch for sidecars that keep a
single generation (overwrite one file forever): a failed write then
truncates the only copy. A check alerting on "same filename as yesterday" is
cheap insurance.

### Tier 4: offsite

`source: copy` sets read the tier 2 history (fast, no load on the data disk).
Data too big for the copy pool (photo originals, a user's cloud drive) goes
in a `source: data` set, read from the run's frozen snapshot. The first seed
of a large set takes nights at a sane upload limit: seed it over several
nights, then the daily increment is small. gzip defeats restic dedup; dump
with `gzip --rsyncable` (or not compressed) where you can.

`<short_name> prune` (weekly) runs `restic forget` per set and app grouped by
host (the frozen path differs every night, so the default host+paths grouping
would keep every snapshot forever), then `restic prune`.

### Tier 5: restore test

Monthly (first Sunday): for every set/app, the newest offsite snapshot is
streamed back file by file (`restic dump`, no disk used) for `samples`
random files, and each sha256 is compared with the SAME file in the frozen
snapshot it was taken from (`src:` tag). If that snapshot has been pruned
the files are restored but not compared (reported as a note). Then
`restic check --read-data-subset`, then the `hooks`. The report lands in
`<state_dir>/reports/restore-test-<ts>.txt` and is sent as `info` (pass) or
`crit` (fail).

### Tier 6: checks and alerts

`<short_name> check` (hourly) reads the artifacts, not the engine's own
success stamps: the newest tier 1 snapshot, the newest history snapshot per
copied app, the newest dump per check, the newest restic snapshot per
set/app AS THE REPOSITORY REPORTS IT (an unreachable repository is a crit),
the age of the last passing restore test, and pool capacity. One alert per
finding, keyed `<short_name>-<check>[-<item>]` for de-duplication. Exit 0 all
fresh, 1 warnings, 2 critical (the unit then shows failed). The report is in
`<state_dir>/reports/check-latest.txt`.

An existing alert CLI plugs in as a command, for example:

```yaml
alerts:
  notifier: command
  command: /opt/alerts/bin/send-alert -l {level} -k {key} {message}
```

Use absolute paths: timers do not have your login PATH.

## CLI

```
<short_name> run                  nightly pipeline (the run timer)
<short_name> snapshot             tier 1, builtin engine
<short_name> predeploy PURPOSE    pre-deploy snapshot
<short_name> copy | offsite       one tier alone
<short_name> prune                restic forget + prune
<short_name> restore-test         tier 5
<short_name> restore ...          see below
<short_name> check                tier 6
<short_name> status               what is on and how old each tier is
<short_name> snapshots            offsite snapshots of this host
<short_name> init-offsite         create the restic repository if missing
```

Exit codes: 0 ok, 1 failure, 2 usage or config error (and crit findings for
`check`), 75 another run holds the lock.

## Restore procedures

Restore BESIDE the live data, check it, then swap it in under maintenance
with the app stopped. The `restore` command refuses a target inside the
live data path and a non-empty target.

**Tier 1 (a file or an app from a data snapshot):**

```bash
zfs list -t snapshot -o name,creation tank/data | tail
<short_name> restore snapshot <app> autosnap_2027-01-15_08:00:01_hourly /restore/<app>
# or by hand: rsync -a /tank/data/.zfs/snapshot/<snap>/<app>/ /restore/<app>/
```

For a large restore, `zfs clone tank/data@<snap> tank/restore` is instant.
Never `zfs rollback` the whole data dataset: it rolls back every app.

**Tier 2 (the data disk is gone or corrupt):**

```bash
zfs list -t snapshot -o name backup/<app>
<short_name> restore copy <app> <short_name>-daily-20270115T053000Z /restore/<app>
```

The copy pool holds the per-app trees; with the data disk replaced, restore
each app into the new data dataset and restart its stack.

**Tier 3 (a database):** restore the app's dump directory with tier 1, 2 or 4,
then load the dump with that service's own restore procedure (`pg_restore`,
`mysql <`). Raw database files from a snapshot are a fallback (crash
recovery on start).

**Tier 4 (the box is gone):** on any machine with restic, the repository
URL, the credentials and the password:

```bash
export RESTIC_REPOSITORY=... RESTIC_PASSWORD_FILE=... AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
restic snapshots --host <host_label> --tag set:<set>,app:<app>
restic restore <id> --target /restore/<app>      # the tree starts at the app's root
```

On a rebuilt storage host with the deploy re-run:
`<short_name> restore offsite <set> <app> /restore/<app> [<id>]`.

**Tier 5** is the rehearsal of tier 4: read its report monthly.

## Choosing apps

- **Keep (tier 2 and 4):** app config and secrets (rendered env, keys, TLS
  state such as an ACME store: losing it can mean nothing re-obtains a
  wildcard certificate), the newest DB dump per app, mail, git repositories
  and LFS, user files, anything a person made.
- **Offsite only:** irreplaceable data too big for the copy pool: photo
  originals, a user's cloud drive. If there is NO other copy, it must be
  offsite.
- **Skip:** anything regenerable: thumbnails, previews, transcoded video,
  model caches, package registries a CI re-publishes, runner caches, search
  indexes, the old dump generations tier 1 already holds, logs and metrics
  you would not restore.
- **Raw database directories:** skip in tier 2/4 when a dump exists (tier 1
  still holds them crash-consistent). A database on a node-local volume is
  NOT in the data snapshot at all: only its dump is.
- **Config directories contain secrets:** the copy pool must be root-only
  (0700) and offsite must be encrypted (restic is).

## B2 setup (Backblaze, S3 API)

1. **Private bucket**, default encryption (SSE-B2) on. restic encrypts
   client-side anyway; SSE is defence in depth.
2. **Lifecycle: keep only the last version** of each file. restic deletes
   objects on prune; with the default "keep all versions", deleted objects
   stay billable forever. (A longer "days before hiding/deleting" gives an
   undo window against a compromised key, at a storage cost.)
3. **Application key scoped to that one bucket**, read and write, no
   `listAllBucketNames` needed. Never use the master key on a host. Put the
   key ID and key in `offsite.env` as `AWS_ACCESS_KEY_ID` /
   `AWS_SECRET_ACCESS_KEY` and use the bucket's S3 endpoint:
   `repository: s3:https://s3.<region>.backblazeb2.com/<bucket>/<host>`.
4. **Raise the caps before the first seed.** A new account has a free tier
   with daily caps: storage, download bandwidth, AND transactions (class B:
   downloads/gets, class C: list and other calls). restic's `check`, `prune`
   and `snapshots` are transaction-heavy; hitting a cap makes the backend
   return errors mid-run. Set caps above expected use (or remove them) and
   enable the cap alerts.
5. Store the repository password and the key in your secret store with an
   offline copy. Test `restore offsite` from a machine that is not the
   storage host.

Any S3-compatible target (a self-hosted object store on ANOTHER site, Wasabi,
S3) works the same way; a same-site object store protects against deletion,
not against losing the site.

## White-labelling

`skbackup.branding` follows the framework's vanity-layer pattern
(`v2/skwire/branding.py`): a name, a tagline, a logo, optional credits.

| Key | Default | Where it shows |
|---|---|---|
| `product_name` | `SKBackup` | CLI banner, help, status, reports, unit `Description=`, config header. |
| `short_name` | `skbackup` | The command, `/usr/local/lib/<short_name>`, `/etc/<short_name>`, `/var/lib/<short_name>`, unit names, alert keys, snapshot names, the always-on restic tag. `^[a-z][a-z0-9-]{1,30}$`. |
| `tagline` | the SKStacks tagline | Banner and help. |
| `alert_prefix` | `SKBackup` | Every alert subject: `[<alert_prefix>] crit: ...`. |
| `report_footer` | `SKBackup, part of SKStacks` | Footer of every alert body and report. |
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

The host then has `/usr/local/sbin/acmevault`, `acme-acmevault-run.timer`
("Acme Vault: nightly dump hooks, ..."), alerts `[ACME-VAULT] crit: ...` and
reports signed by Acme IT. The engine carries no brand string of its own
(it prints only what the config says), and the test suite renders the
deploy in white-label mode and fails if any default brand string appears in
any rendered file, install path, shell body, or anything the engine prints,
alerts or reports (`v1/tests/test_skbackup_whitelabel.py`). Changing
`short_name` on an existing host installs under the new name: remove the old
units (`systemctl disable --now <old>-*.timer`) and paths by hand.

## Upgrading from the Duplicati skbackup

The Duplicati app (v2.19.0 to v2.22.0) was never deployed on an instance, so
there is nothing to migrate. On instances whose vault still carries its keys
(`settings_encryption_key`, `ui_password`, `passphrase`, `jobs`, `exclude`,
`DUPLICATI_VERSION`, `source_path`, `source_read_only`, `CLI_ARGS`), remove
them: the deploy refuses them rather than ignore them. `CLUSTERNAME`,
`DOMAIN`, `ACME_ENABLED` and `networks` are simply unused now. If an old
private Duplicati copy left state on NFS (`/var/data/skbackup-<env>`,
`/var/data/config/skbackup-<env>`) and no job ever wrote to a target, archive
it once and delete it. No overlay network or swarm stack is needed.

## Lessons learned

- **A copy on the same disk is not a backup.** Bulk copies into a backups
  directory on the NFS pool doubled the space and protected against nothing
  that loses the disk. A snapshot gives the same point-in-time safety
  instantly, at zero I/O.
- **Heavy I/O on the shared pool is a prod change.** An unthrottled bulk copy
  of one app's data saturated the NFS server and outran another service's
  health check (a long outage). Backups read from snapshots, run locally on
  the storage host, capped, at a fixed night slot, never during deploys.
- **One box can be all of prod.** When the NFS pool and every swarm VM share a
  host, only an off-box copy survives that host. And a single-disk pool can
  detect corruption but not repair it: a mirror is not a backup, but it
  removes the largest single risk cheaply.
- **Snapshots pin deleted data.** Decide on legacy data before retention
  starts.
- **Dumps: check the artifact.** Sidecars that expand the date once at start
  overwrote one file forever; a dump failing mid-write then destroys the only
  copy. Freshness plus non-empty plus "a new file" catches it. A database on
  a node-local volume is invisible to the storage host's snapshots.
- **"Succeeded" is not "exists".** The check reads the repository, not the
  engine's stamps; the restore test reads the offsite copy, not the local one.
- **Prune must group by host.** With a changing frozen path, restic's default
  host+paths grouping keeps every snapshot forever.
- **Schedulers have no PATH.** Notifier commands use absolute paths.
- **Keep the password offline.** A restic repository without its password is
  noise.

## Testing

- `v1/tests/test_skbackup_render.py`: the real deploy playbook through Ansible
  (default and white-label), fail-closed validation.
- `v1/tests/test_skbackup_whitelabel.py`: the brand-leak scan.
- `v1/tests/test_skbackup_engine.py`: the engine on a scratch tree with the
  `dir` backend and a local restic repository (backup, restore, sha256,
  retention, locking, dump gates, staleness, notifier).
- `v1/tests/test_skbackup_shellcheck.py`: `bash -n` and shellcheck.
- The render gate renders `vars/skbackup.example.yml` (default brand) and
  `vars/skbackup.set.yml` (every tier, white-label brand).
