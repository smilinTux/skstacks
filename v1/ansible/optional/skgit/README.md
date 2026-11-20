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
4. **Stop the app again, drop the fresh database, restore.** Step 3 started Forgejo too, and it ran its
   migrations on the empty 18 cluster, so a restore on top collides with that schema (not only the
   bootstrap role). The backup sidecar would also dump the empty interim database if it fired now.
   Stop both, drop the fresh database, then restore the whole dump:

   ```bash
   docker service scale <stack>_forgejo=0 <stack>_db-backup=0
   C=$(docker ps -q -f name=<stack>_postgres)
   docker exec "$C" psql -U forgejo -d postgres \
     -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'forgejo' AND pid <> pg_backend_pid()" \
     -c 'DROP DATABASE "forgejo"'
   ```

   Then restore into the new cluster:

   ```bash
   C=$(docker ps -q -f name=<stack>_postgres)
   docker exec -i "$C" psql -U forgejo -d postgres < /var/data/skgit-<env>/database-dump/pre-pg18.sql 2> /tmp/restore.err
   grep -v "already exists" /tmp/restore.err | head    # anything else here needs a look
   ```

   An `already exists` error for the bootstrap role is expected: `initdb` created it. The dump
   re-creates the database itself, so an error about its tables means the drop above did not run.
5. **Verify:** `docker exec "$C" psql -U forgejo -c "SHOW server_version" -c "SHOW data_checksums"`, then bring the
   writers back (`docker service scale <stack>_forgejo=1 <stack>_db-backup=1`) and check the Forgejo health endpoint and a real login or request.
6. **Clean up** after a few days of good operation: remove `/var/data/runtime/skgit-<env>/postgres.pg16` and the dump file.

**Rollback.** The old directory is untouched. `docker stack rm <stack>`, move `/var/data/runtime/skgit-<env>/postgres` aside,
move `/var/data/runtime/skgit-<env>/postgres.pg16` back to `/var/data/runtime/skgit-<env>/postgres`, pin `skgit.POSTGRES_IMAGE` and `skgit.POSTGRES_BACKUP_IMAGE` to the 16 image
(see above), redeploy. Data written after the upgrade is not carried back; restore it from a dump if needed.

Order matters: if the deploy runs before step 2 the guard stops it with the message that names this
section. That is the intended behaviour, not a fault.

## Registration and anonymous read

`skgit.DISABLE_REGISTRATION` and `skgit.REQUIRE_SIGNIN_VIEW` (both optional)
render Forgejo's `[service]` section. Unset, Forgejo's defaults apply: anyone
can register and anonymous visitors can read every public repo. An
internet-facing instance should set both to `true`. Users are then created by
an admin (web UI or `POST /api/v1/admin/users`), and API or git access needs a
token or SSH key.

## Git pack memory limits

`app.ini` always renders a `[git.config]` section, which Forgejo writes into
its own git home at startup, so every repack, gc and upload-pack it spawns is
bounded: `pack.threads = 1`, `pack.windowMemory = 256m`,
`pack.deltaCacheSize = 64m`, `core.deltaBaseCacheLimit = 64m`, `gc.auto = 0`
(Forgejo's own cron runs housekeeping). Without these, concurrent repacks of
large repositories took nor's Forgejo past its memory limit (OOM, 2026-11-11).
`skgit.GIT_CONFIG` is an optional dict of git config `key: value`; a key there
replaces the default or adds a new one. Check live with
`docker exec <forgejo> git config --global --list | grep -E 'pack|gc'`.

## Sign-in through Authentik (OIDC)

`skgit.AUTH_MODE` is `local` by default: Forgejo keeps its own accounts and the render is
unchanged. `AUTH_MODE: oidc` makes the deploy create, or update in place, **one** Forgejo
OAuth2 auth source (provider `openidConnect`) that signs users in through Authentik
(sksso). It adds a sign-in option; local password login and local accounts stay.

What the deploy does, after the stack is up and the Forgejo task is Running:

1. Waits for the Forgejo API (the same wait as the admin bootstrap).
2. Runs `oidc-auth-source.sh` on the node that runs Forgejo. It lists the sources with
   `forgejo admin auth list` and finds yours **by name**: found (and of type OAuth2), it runs
   `forgejo admin auth update-oauth --id N`; absent, `add-oauth`. It never deletes anything
   and never touches a source of another name. A same-named source of another type
   (LDAP, for example) stops the deploy with a message instead of being overwritten.
3. The client secret is handed to the container by environment variable name, so it is in
   no `docker` argument list and in no output (failures print with it redacted, and the
   task is `no_log`). It is stored in Forgejo's database, never in `app.ini`.

The update runs on every deploy, so rotating the secret in both vaults and redeploying
(sksso, then skgit) takes effect without any manual step.

```yaml
skgit:
  AUTH_MODE: oidc                                # local (default) or oidc
  OIDC_CLIENT_SECRET: "..."                      # 32+ chars, same value as sksso.SKGIT_OIDC_CLIENT_SECRET
  OIDC_SOURCE_NAME: "SKStack SSO"                # auth source name, same value as sksso.SKGIT_OIDC_SOURCE_NAME
  # OIDC_CLIENT_ID: skgit                        # default AUTHENTIK_TILE_SLUG, else skgit (the Authentik application slug)
  # OIDC_SSO_HOST: sso.example.org               # default sso[-<env>].<cluster>.<domain>
  # OIDC_DISCOVERY_URL: ""                       # full override of the discovery URL
  # OIDC_AUTO_REGISTRATION: false                # create a Forgejo account on first SSO login
  # OIDC_ACCOUNT_LINKING_BY_EMAIL: false         # link to the account with the same email, no password prompt
  # OIDC_USERNAME_CLAIM: preferred_username      # username for auto-registered accounts (userid, nickname, email, preferred_username)
```

- **Which vault holds the secret.** `skgit.OIDC_CLIENT_SECRET` in the skgit vault, and the
  same value as `sksso.SKGIT_OIDC_CLIENT_SECRET` in the sksso vault. Generate it once
  (`openssl rand -hex 32`) and set it in both.
- **Discovery URL.** `https://<sso host>/application/o/<client id>/.well-known/openid-configuration`.
  The Forgejo container must reach that host (through the public name and Traefik).
- **Blueprint on the sksso side.** Set `sksso.SKGIT_OIDC_ENABLED: true` and the matching
  secret and source name there first (`../sksso/README.md` "skgit (Forgejo) OIDC login").
  The Application slug equals this role's portal tile slug, so there is one Application;
  who may sign in is `AUTHENTIK_TILE_GROUP` (default `skadmin`).
- **CapAuth.** Set `sksso.SKGIT_OIDC_AUTHENTICATION_FLOW: capauth-authentication` on the
  sksso side to make this sign-in a PGP, passwordless login. Nothing changes here.
- **Policy keys (`app.ini`, `[oauth2_client]`).** Rendered only in oidc mode.
  `OIDC_AUTO_REGISTRATION` (default off): without it a first SSO login lands on Forgejo's
  "link to an existing account or register" page. `OIDC_ACCOUNT_LINKING_BY_EMAIL` (default
  off, renders `ACCOUNT_LINKING = login`; on renders `auto`): a first SSO login is linked
  to the Forgejo account with the same email without asking for its password. Turn it on
  only when Authentik is the only identity provider and its emails are trusted, because an
  account is taken over by whoever controls that email at the provider. With auto
  registration on, `OIDC_USERNAME_CLAIM` (default `preferred_username`) names the account.
  How these interact with `DISABLE_REGISTRATION` is Forgejo's own behaviour: test a
  first sign-in of a new user after enabling.
- **Rollback.** Set `AUTH_MODE: local` and redeploy: the deploy stops managing the source
  but does not delete it. Remove it in Forgejo (Site Administration, Authentication
  Sources) or with `forgejo admin auth delete --id N`, only once nobody depends on it.
