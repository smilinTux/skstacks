# skorch (n8n)

Workflow orchestration with a pgvector Postgres, redis and a `postgres-backup-local` sidecar. Router middleware chain: vault knob `TRAEFIK_MIDDLEWARES` (default `default-no-crowdsec@file`). Postgres data is bind-mounted from `/var/data/runtime/skorch-<env>/postgres`.

## Upgrading Postgres to 18

Framework default: `postgres:18.6` (pinned `tag@sha256:digest`) (`pgvector/pgvector:pg18` for the server, `smilintux/postgres-backup-local:18-<yyyymmdd>` for backups, built from [`image/`](./image/): the backup image major must match the server major). Earlier releases ran Postgres 16.
**Postgres 18 cannot open a Postgres 16 data directory, and an upgrade is never automatic.**

What the framework does for you:

- **Fail-closed guard.** Before the stack deploy, `shared/tasks/postgres_major_guard.yml` reads
  `PG_VERSION` under `/var/data/runtime/skorch-<env>/postgres` and stops the play when the data on disk is another major than the
  image to deploy. The running stack is left untouched. Fresh installs (no `PG_VERSION`) pass.
- **New data layout.** 18+ images keep the cluster in `/var/lib/postgresql/<major>/docker` and declare
  `VOLUME /var/lib/postgresql`. For an 18+ image the compose file therefore mounts the data
  directory at `/var/lib/postgresql` (a fresh install persists at `/var/data/runtime/skorch-<env>/postgres/18/docker`). A
  pinned older image keeps the old mount target `/var/lib/postgresql/data`. Never mount
  `/var/lib/postgresql/data` under an 18 image: the real data would land on an anonymous volume.
  The guard is also what stops an 18 image from initialising an empty database beside old files.
- **Keep your current major.** Pin both images in the vault and redeploy; nothing else changes:

  ```yaml
  skorch:
    POSTGRES_IMAGE: "pgvector/pgvector:pg16@sha256:<digest>"
    POSTGRES_BACKUP_IMAGE: "smilintux/postgres-backup-local:16-20261128@sha256:3440139130c4c834df1f483fce24fce138ce0bc7e870268f0fb5f312fc29da99"   # the dump sidecar must match the server major
  ```

Upstream support: pgvector publishes `pg18` images built on the official `postgres:18` image, so the 18 layout above applies unchanged. The backup sidecar is `smilintux/postgres-backup-local:18-<yyyymmdd>`, matching the server major (see "Image").

PG18 defaults worth knowing (no action needed): `initdb` enables data checksums by default (a
dump-and-restore creates a checksummed cluster, a small CPU cost); `io_method` defaults to `worker`
(asynchronous I/O through background workers, no `io_uring` unless you set it, and no memory limit
change is needed); `md5` password hashes still work but log a deprecation warning, re-set the
password to move to SCRAM. `PGTEMP` in the compose file is not a Postgres setting and is ignored.

### Runbook: dump and restore (16 to 18)

Plan for a short outage (skorch is down from step 2 to step 6). All commands run on the swarm
manager; `<env>` is your environment, `<stack>` is `skorch-<env>`.

1. **Dump from the running 16.** Stop the writers first so the dump is consistent, then dump the
   whole cluster with the old server's own tools:

   ```bash
   docker service scale <stack>_n8n=0
   C=$(docker ps -q -f name=<stack>_postgres)
   docker exec "$C" pg_dumpall -U <POSTGRES_USER from postgres.env> > /var/data/skorch-<env>/database-dump/pre-pg18.sql
   ls -l /var/data/skorch-<env>/database-dump/pre-pg18.sql
   tail -n 2 /var/data/skorch-<env>/database-dump/pre-pg18.sql   # must end "PostgreSQL database cluster dump complete"
   ```

   The file holds password hashes: keep it `0600` and delete it once the upgrade is verified.
2. **Stop the stack and move the old data aside** (this is your rollback copy, do not delete it yet):

   ```bash
   docker stack rm <stack>
   mv /var/data/runtime/skorch-<env>/postgres /var/data/runtime/skorch-<env>/postgres.pg16
   mkdir -p /var/data/runtime/skorch-<env>/postgres && chown 999:999 /var/data/runtime/skorch-<env>/postgres
   ```

3. **Pin nothing and deploy.** With the default image, run the usual deploy for `skorch`. The guard
   sees an empty directory (fresh install) and passes; the 18 image initialises `/var/data/runtime/skorch-<env>/postgres/18/docker`
   from the credentials in the stack's `postgres.env`/vault. Wait for the `postgres` service to report healthy.
4. **Stop the app again, drop the fresh database, restore.** Step 3 started n8n too, and it ran its
   migrations on the empty 18 cluster, so a restore on top collides with that schema (not only the
   bootstrap role). The backup sidecar would also dump the empty interim database if it fired now.
   Stop both, drop the fresh database, then restore the whole dump:

   ```bash
   docker service scale <stack>_n8n=0 <stack>_postgres-backup=0
   C=$(docker ps -q -f name=<stack>_postgres)
   docker exec "$C" psql -U <POSTGRES_USER from postgres.env> -d postgres \
     -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '<POSTGRES_DB from postgres.env>' AND pid <> pg_backend_pid()" \
     -c 'DROP DATABASE "<POSTGRES_DB from postgres.env>"'
   ```

   Then restore into the new cluster:

   ```bash
   C=$(docker ps -q -f name=<stack>_postgres)
   docker exec -i "$C" psql -U <POSTGRES_USER from postgres.env> -d postgres < /var/data/skorch-<env>/database-dump/pre-pg18.sql 2> /tmp/restore.err
   grep -v "already exists" /tmp/restore.err | head    # anything else here needs a look
   ```

   An `already exists` error for the bootstrap role is expected: `initdb` created it. The dump
   re-creates the database itself, so an error about its tables means the drop above did not run.
   The pgvector extension is created by the init script on the empty cluster, so its `already exists` error is expected too.
5. **Verify:** `docker exec "$C" psql -U <POSTGRES_USER from postgres.env> -c "SHOW server_version" -c "SHOW data_checksums"`, then bring the
   writers back (`docker service scale <stack>_n8n=1 <stack>_postgres-backup=1`) and check the skorch health endpoint and a real login or request.
6. **Clean up** after a few days of good operation: remove `/var/data/runtime/skorch-<env>/postgres.pg16` and the dump file.

**Rollback.** The old directory is untouched. `docker stack rm <stack>`, move `/var/data/runtime/skorch-<env>/postgres` aside,
move `/var/data/runtime/skorch-<env>/postgres.pg16` back to `/var/data/runtime/skorch-<env>/postgres`, pin `skorch.POSTGRES_IMAGE` and `skorch.POSTGRES_BACKUP_IMAGE` to the 16 image
(see above), redeploy. Data written after the upgrade is not carried back; restore it from a dump if needed.

Order matters: if the deploy runs before step 2 the guard stops it with the message that names this
section. That is the intended behaviour, not a fault.

## Image: the backup sidecar

The sidecar default is `smilintux/postgres-backup-local:18-20261128@sha256:cefa1d9171dedbb0...`, built from
[`image/`](./image/). It replaces `prodrigestivill/postgres-backup-local`, whose last build (2025-09-26) carried
fixable CRITICAL and HIGH findings in its Debian base, in the Postgres client libraries and in two prebuilt Go
binaries (Trivy 0.76.2, `--severity CRITICAL,HIGH --ignore-unfixed`: 20 CRITICAL and 292 HIGH on the 16 image,
20 and 232 on the 18 image; 0 and 0 on the rebuilds). It is a drop-in: the same `/init.sh` entrypoint, the same
env vars (`POSTGRES_*`, `SCHEDULE`, `BACKUP_KEEP_*`, `HEALTHCHECK_PORT`, `WEBHOOK_*`), the same `/backups`
layout and the same healthcheck, so `postgres-backup.env` and the compose service are unchanged.

What `image/` does differently: it starts from a digest-pinned `postgres:<major>-trixie`, runs
`apt-get -y upgrade`, builds `go-cron` and `gosu` from pinned source with a current Go toolchain (the prebuilt
upstream binaries carried the Go standard library findings), and vendors upstream's `backup.sh`, `env.sh`,
`init.sh` and `hooks/` unchanged (`image/UPSTREAM.md` names the commit).

Published tags, one per Postgres major (amd64, by manifest digest):

| Tag | Digest |
| --- | --- |
| `smilintux/postgres-backup-local:18-20261128` (framework default) | `sha256:cefa1d9171dedbb08c03ab69d23ddd5793aa5b7405a213d90c80f9bcd16f1da9` |
| `smilintux/postgres-backup-local:16-20261128` | `sha256:3440139130c4c834df1f483fce24fce138ce0bc7e870268f0fb5f312fc29da99` |

An instance that kept Postgres 16 pins the 16 tag in `skorch.POSTGRES_BACKUP_IMAGE` (shown above). An instance
that wants its own build sets `skorch.POSTGRES_BACKUP_IMAGE` to its own reference; the override always wins.

Rebuild (after a Debian or Postgres security update, and whenever you take a newer upstream), one build per major:

```bash
cd v1/ansible/optional/skorch
# Postgres 18 (the default); look the base digest up with: docker pull postgres:18-trixie
docker build --build-arg PG_BASE=postgres:18-trixie@sha256:<digest> \
  -t smilintux/postgres-backup-local:18-$(date +%Y%m%d) image/
# Postgres 16: the Dockerfile's default base is 16
docker build -t smilintux/postgres-backup-local:16-$(date +%Y%m%d) image/
# Scan, then smoke test: one dump against a throwaway Postgres of the same major
# (BACKUP_ON_START=TRUE, check /backups/daily/<db>-<date>.sql.gz and the health port).
docker push smilintux/postgres-backup-local:18-<yyyymmdd>
```

Pin the pushed manifest digest (`docker push` prints it) in `skorch.yml.j2` as the `POSTGRES_BACKUP_IMAGE`
default and in the table above, in one commit. The `PG_BASE` default and the `golang` base in `image/Dockerfile`
are digest-pinned; refresh them in the same commit when you rebuild.

## API router (raw status codes for machine traffic)

The router chain usually ends in `error-pages@file`, which replaces any backend 4xx/5xx with an HTML page and
replays the original method there, so API clients never see the app's own status or JSON error. `skorch[-env]-api` carries the
machine paths (`/webhook` (production, test and waiting webhooks), `/api/`, `/rest/`, `/mcp`, `/healthz`, `/metrics`) through the same chain with each error-pages-bearing chain swapped for its
no-error-pages form; anything else in the chain (CrowdSec, SSO, custom middlewares) is kept. Vault key
`API_MIDDLEWARES` replaces the derived chain whole. `default-no-error-pages@file` has neither `default@file`'s
optional IP allowlist nor CrowdSec: set `API_MIDDLEWARES` if the main chain relies on them. See "API routers" in
`docs/v1-service-catalog.md`.
