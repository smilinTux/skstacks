# skgit (Forgejo)

Forgejo git hosting with a Postgres database, a dump sidecar and optional Actions runners. Router middleware chain: vault knob `TRAEFIK_MIDDLEWARES` (default `default-no-crowdsec@file`). API and git traffic (`/api/`, `info/refs`, `git-upload-pack`, `git-receive-pack`, `info/lfs/`) goes through the `skgit[-env]-api` router, whose chain is the main one without error pages (vault knob `API_MIDDLEWARES` replaces it), so clients get Forgejo's own status codes and JSON errors instead of the HTML error page. Postgres data is bind-mounted from `/var/data/runtime/skgit-<env>/postgres`.

## Upgrading Postgres to 18

Framework default: `postgres:18.6` (pinned `tag@sha256:digest`) for the server and the dump sidecar. Earlier releases ran Postgres 16.
**Postgres 18 cannot open a Postgres 16 data directory, and an upgrade is never automatic.**

What the framework does for you:

- **Fail-closed guard.** Before the stack deploy, `shared/tasks/postgres_major_guard.yml` reads
  `PG_VERSION` under `/var/data/runtime/skgit-<env>/postgres` and stops the play when the data on disk is another major than the
  image to deploy. The running stack is left untouched. Fresh installs (no `PG_VERSION`) pass.
- **New data layout.** 18+ images keep the cluster in `/var/lib/postgresql/<major>/docker` and declare
  `VOLUME /var/lib/postgresql`. For an 18+ image the compose file therefore mounts the data
  directory at `/var/lib/postgresql` (a fresh install persists at `/var/data/runtime/skgit-<env>/postgres/18/docker`). A
  pinned older image keeps the old mount target `/var/lib/postgresql/data`. Never mount
  `/var/lib/postgresql/data` under an 18 image: the real data would land on an anonymous volume.
  The guard is also what stops an 18 image from initialising an empty database beside old files.
- **Keep your current major.** Pin both images in the vault and redeploy; nothing else changes:

  ```yaml
  skgit:
    POSTGRES_IMAGE: "postgres:16.15@sha256:<digest>"
    POSTGRES_BACKUP_IMAGE: "postgres:16.15@sha256:<digest>"   # the dump sidecar should match the server major
  ```

Upstream support: Forgejo documents PostgreSQL 14 and later as supported, so 18 is within its range.

PG18 defaults worth knowing (no action needed): `initdb` enables data checksums by default (a
dump-and-restore creates a checksummed cluster, a small CPU cost); `io_method` defaults to `worker`
(asynchronous I/O through background workers, no `io_uring` unless you set it, and no memory limit
change is needed); `md5` password hashes still work but log a deprecation warning, re-set the
password to move to SCRAM. `PGTEMP` in the compose file is not a Postgres setting and is ignored.

### Runbook: dump and restore (16 to 18)

Plan for a short outage (Forgejo is down from step 2 to step 6). All commands run on the swarm
manager; `<env>` is your environment, `<stack>` is `skgit-<env>`.

1. **Dump from the running 16.** Stop the writers first so the dump is consistent, then dump the
   whole cluster with the old server's own tools:

   ```bash
   docker service scale <stack>_forgejo=0
   C=$(docker ps -q -f name=<stack>_postgres)
   docker exec "$C" pg_dumpall -U forgejo > /var/data/skgit-<env>/database-dump/pre-pg18.sql
   ls -l /var/data/skgit-<env>/database-dump/pre-pg18.sql
   tail -n 2 /var/data/skgit-<env>/database-dump/pre-pg18.sql   # must end "PostgreSQL database cluster dump complete"
   ```

   The file holds password hashes: keep it `0600` and delete it once the upgrade is verified.
2. **Stop the stack and move the old data aside** (this is your rollback copy, do not delete it yet):

   ```bash
   docker stack rm <stack>
   mv /var/data/runtime/skgit-<env>/postgres /var/data/runtime/skgit-<env>/postgres.pg16
   mkdir -p /var/data/runtime/skgit-<env>/postgres && chown 999:999 /var/data/runtime/skgit-<env>/postgres
   ```

3. **Pin nothing and deploy.** With the default image, run the usual deploy for `skgit`. The guard
   sees an empty directory (fresh install) and passes; the 18 image initialises `/var/data/runtime/skgit-<env>/postgres/18/docker`
   from the credentials in the stack's `postgres.env`/vault. Wait for the `postgres` service to report healthy.
4. **Restore into the new cluster:**

   ```bash
   C=$(docker ps -q -f name=<stack>_postgres)
   docker exec -i "$C" psql -U forgejo -d postgres < /var/data/skgit-<env>/database-dump/pre-pg18.sql 2> /tmp/restore.err
   grep -v "already exists" /tmp/restore.err | head    # anything else here needs a look
   ```

   `already exists` errors for the bootstrap role and database are expected: `initdb` created them.
5. **Verify:** `docker exec "$C" psql -U forgejo -c "SHOW server_version" -c "SHOW data_checksums"`, then bring the
   writers back (`docker service scale <stack>_forgejo=1`) and check the Forgejo health endpoint and a real login or request.
6. **Clean up** after a few days of good operation: remove `/var/data/runtime/skgit-<env>/postgres.pg16` and the dump file.

**Rollback.** The old directory is untouched. `docker stack rm <stack>`, move `/var/data/runtime/skgit-<env>/postgres` aside,
move `/var/data/runtime/skgit-<env>/postgres.pg16` back to `/var/data/runtime/skgit-<env>/postgres`, pin `skgit.POSTGRES_IMAGE` and `skgit.POSTGRES_BACKUP_IMAGE` to the 16 image
(see above), redeploy. Data written after the upgrade is not carried back; restore it from a dump if needed.

Order matters: if the deploy runs before step 2 the guard stops it with the message that names this
section. That is the intended behaviour, not a fault.
