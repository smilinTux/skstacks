# skbackup (Duplicati)

The framework's own backup core service. v2's platform architecture already
names a backup core service; nothing implemented it, so every optional
stack on this estate grew its own DB-dump sidecar instead (skorch's
`postgres-backup`, skgallery's `postgres-backup`, skgit's Postgres dumps,
and so on). skbackup does not replace those sidecars -- it is what backs
**them up**, along with everything else on the shared filesystem: a single
[Duplicati](https://duplicati.com/) instance on Docker Swarm that reads
`/var/data` read-only and ships encrypted, deduplicated snapshots to a
destination this instance controls.

## Vault contract

Required (no default -- the deploy fails closed without these):

| Key | Notes |
|---|---|
| `skbackup.CLUSTERNAME` | Used to build the public hostname `skbackup[-env].<cluster>.<domain>`. |
| `skbackup.DOMAIN` | Same. |
| `skbackup.settings_encryption_key` | Duplicati's own settings-database encryption key (linuxserver image env `SETTINGS_ENCRYPTION_KEY`, min 8 alphanumeric characters). No default: the upstream image itself only requires this when you *don't* want an unencrypted settings DB, but this framework requires it unconditionally. |
| `skbackup.ui_password` | The web UI login password (linuxserver image env `DUPLICATI__WEBSERVICE_PASSWORD`). **No default here on purpose**: the upstream image defaults this to the literal string `changeme` when the env var is unset, which this template never falls back to. The deploy playbook also asserts this is non-empty (a vault value of `""` is caught there; an unset vault key is caught by Jinja's undefined lookup on the template itself). |
| `skbackup.passphrase` | Instance-wide default Duplicati encryption passphrase, used by any job in `skbackup.jobs` that doesn't set its own `passphrase`. Required if any job is declared without a per-job passphrase -- the deploy playbook asserts every declared job resolves to a non-empty passphrase and fails closed otherwise. Not needed if `skbackup.jobs` is empty (the common case: jobs set up by hand in the UI carry their own passphrase, entered interactively). |

Optional (sane defaults, see `src/config/skbackup/*.j2`):

- `skbackup.DUPLICATI_VERSION` -- image tag@digest (default pinned, see "Image" below).
- `skbackup.source_path` (default `/var/data`) -- the host path bind-mounted into the container.
- `skbackup.source_read_only` (default `true`) -- set `false` only if you have a specific reason to let Duplicati write into the source tree (it never needs to).
- `skbackup.exclude` -- a list of raw Duplicati filter-file lines, replacing the built-in defaults (own dir, `runtime/*`, caches -- see "What this backs up" below).
- `skbackup.jobs` -- declarative backup jobs, see "Declarative jobs" below. Default: `[]` (none; set jobs up by hand in the web UI instead).
- `skbackup.ACME_ENABLED` (default `false`) -- attach the shared `main` cert resolver to this service's Traefik router; leave off on instances where ACME isn't configured (Traefik then serves its own default self-signed certificate for this router, same as every other v1 service without ACME).
- `skbackup.CPU_LIMIT` / `skbackup.MEMORY_LIMIT` / `skbackup.CPU_RESERVATION` / `skbackup.MEMORY_RESERVATION`, `skbackup.TZ`, `skbackup.PUID` / `skbackup.PGID`, `skbackup.INSTANCE`, `skbackup.APP_ENV`, `skbackup.CLOUDFLARED`, `skbackup.networks` (overrides the registered `skbackup-<env>` / `cloud-public-<env>` overlay networks), `skbackup.CLI_ARGS` (passed straight to the linuxserver image's own `CLI_ARGS` env var).

## Image

`lscr.io/linuxserver/duplicati`, pinned to `v2.4.0.0_stable_2026-09-03-ls309`
(Duplicati 2.4.0) by tag and digest. **Not** `duplicati/duplicati`, the
project's own Docker Hub image: its most recent published tag is a 2023
beta (`2.0.7.1_beta_2023-05-25`), i.e. effectively unmaintained as a
distribution channel, while `linuxserver/duplicati` ships regular releases
(2.4.0 seven days before this was written) and is the image the Duplicati
project's own community points people at. See
`docs/decisions/skstor-backend.md` for the same kind of check done for
skstor/Garage vs MinIO.

## What this backs up (and what it doesn't)

The container mounts `skbackup.source_path` (default `/var/data`,
read-only) at `/source`. A rendered filter file
(`/var/data/config/skbackup-<env>/exclude.filter`, mounted read-only at
`/config/exclude.filter`) excludes, by default:

1. **skbackup's own data and config dirs** -- backing up the backup app's
   own state is pointless and would recurse.
2. **every service's live DB/cache runtime datadir**, i.e. everything
   under `/var/data/runtime/<svc>-<env>/*` (Postgres, Redis, MariaDB's
   `db`, ClamAV signatures, Forgejo runner sessions, ...). These are
   **not** backed up here because they are live database/cache engine
   files: copying them mid-write is not a consistent backup, which is
   exactly why services like skorch and skgallery already run their own
   `postgres-backup` sidecar that dumps to a plain file on disk instead.
   skbackup backs up **those dump files** (they live under each service's
   own `/var/data/<svc>-<env>/...` tree, not under `runtime/`), not the
   live engine storage.
3. Generic cache directories (`.cache/`, `cache/`) by name, wherever they
   occur.

Override the whole list with `skbackup.exclude` (a list of raw
[Duplicati filter](https://docs.duplicati.com/detailed-descriptions/filters-and-paths)
lines) if an instance's layout doesn't match this estate's convention.

This default exclude list only applies to jobs rendered by
`skbackup.jobs` (see below), which reference the filter file via
`--filter-file=/config/exclude.filter`. A job created by hand in the web
UI does **not** pick it up automatically -- add the same option under
**Settings -> Default options** (applies to every backup, existing and
future) or per-job under **Options -> Advanced -> `--filter-file`**.

## Declarative jobs

`skbackup.jobs` is a list of:

```yaml
skbackup:
  jobs:
    - name: skstor-database-dumps
      description: "Optional, free text"
      sources:                       # paths under /source, i.e. under skbackup.source_path
        - /skorch-prod/database-backup
        - /skgallery-prod/database-backup
      destination_url: "s3://skbackup-prod/skorch-dumps?s3-server-name=skstor-prod.<cluster>.<domain>&s3-use-ssl=true&auth-username=<access-key>&auth-password=<secret-key>"
      schedule_time: "23:00"
      schedule_repeat: "1D"
      retention: "30D"               # Duplicati keep-time; omit and it defaults to 30D
      passphrase: "..."              # optional; falls back to skbackup.passphrase
```

Each entry is rendered at deploy time into Duplicati's own export/import
JSON schema (the same shape "Export -> As file" produces from the web UI)
at `/var/data/config/skbackup-<env>/jobs/<name>.json`.

**This is rendered, not imported.** Duplicati does have a non-interactive
importer (`ConfigurationImporter`, see
[duplicati/duplicati#3595](https://github.com/duplicati/duplicati/pull/3595)),
but it writes directly into `Duplicati-server.sqlite` and the documented
usage expects the server to be stopped while it runs. This framework's
build process has no live container to check against, so we cannot confirm
that binary ships (or its exact invocation -- `mono` vs `dotnet`, install
path) inside every pinned `linuxserver/duplicati` release, and scripting an
unverified command against a *running* server's database is a real
corruption risk, not a reasonable default for a public framework. So:

- **One-time step per rendered job, per instance:** open the skbackup web
  UI -> **Add backup** -> **Import from a file** -> pick the matching
  `<name>.json` under `/var/data/config/skbackup-<env>/jobs/` (copy it
  somewhere your browser can reach it, or import it from a shell on the
  manager node if the UI supports a server-side path). Duplicati asks
  whether to import "Backup" and/or "Schedule" settings; keep both.
- A future `skbackup.jobs_auto_import` var is the natural place to wire up
  automatic import once `ConfigurationImporter`'s presence and exact
  invocation have been verified against the pinned image tag -- not
  implemented here.

## Destinations

Instance vars only; there is no default that points anywhere real.
`destination_url` is Duplicati's own `TargetURL` connection string --
copy it verbatim from Duplicati's own "Export -> As commandline" for
whatever backend you're using, rather than trusting a hand-typed one, since
the exact query-string keys (`auth-username`, `auth-password`,
`s3-server-name`, ...) are backend- and version-sensitive.

**Pointing at this framework's own skstor (Garage):** create a bucket and
key for skbackup the same way any other skstor consumer does (see
`../skstor/README.md` "One-time bootstrap"), then reach it over the
`s3://` scheme with `s3-server-name=skstor[-env].<cluster>.<domain>` (or,
if skbackup and skstor share this instance's Swarm overlay networks, the
in-cluster service name instead of the public hostname) and
`s3-use-ssl=true`.

Local-path and SFTP destinations are also supported by Duplicati directly
(`file:///backups/...` for a path inside this container's own `/backups`
bind mount, or `ssh://...`); same rule applies, no default destination.

## Restore procedure

1. Open the skbackup web UI, select the job (or **Restore** -> **Direct
   restore from backup files** if the job itself isn't configured on this
   instance -- paste the same `destination_url` and passphrase).
2. Pick the version/point in time, the files/paths to restore, and either
   restore in place or to an alternate path. Duplicati downloads and
   decrypts only the blocks needed for the selected files, not the whole
   backup set.
3. For a DB dump that a sidecar produced (e.g.
   `/skorch-prod/database-backup/*.sql.gz`), restore the dump file, then
   follow that service's own restore procedure for loading it back into
   the live database -- skbackup restores the file, not the database.
4. To verify a destination end-to-end without touching production data,
   `duplicati-cli test <destination_url> --passphrase=... all` checks
   every remote volume's integrity.

## Networks

New overlay subnets, none of which collide with any subnet already
registered on `main`, `integration/*`, or any open `feat/*` branch as of
this service's addition: `skbackup-prod` `172.16.255.0/24`,
`skbackup-staging` `172.16.247.0/24`, `skbackup-dev` `172.16.246.0/24`.
`cloud-public-<env>` reuses the framework's existing shared subnets
(`172.16.200.0/24` / `.201.0/24` / `.202.0/24` for prod/staging/dev).
