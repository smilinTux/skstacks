# skhome

[HomeCenter](https://github.com/yatzin/home-center) on the swarm: a
self-hosted home manager (properties, vehicles, equipment, warranties,
receipts with OCR, costs, maintenance reminders, optional family health
records, an optional AI assistant). Opt-in: nothing renders or changes for
an instance that never runs `deploy_skhome-<env>.yml`.

## Upstream, license and image

- **License.** MIT (`github.com/yatzin/home-center`, website
  `https://home-center.dazzos.com`). This role **references the upstream
  image and vendors nothing**.
- **Image.** `ghcr.io/yatzin/homecenter`, release `2.3.1`, multi-arch
  (amd64, arm64), pinned by the manifest-list digest; the play refuses an
  image without `@sha256:`.
- **No phone-home from the server.** HomeCenter makes no network call by
  default. It goes online only for things an admin turns on: email
  reminders (SMTP), a cloud AI provider (if the assistant is pointed at one
  instead of a local model), or downloading a larger search model.

## Design

### Services

| Service | Image | Replicas | What |
|---|---|---|---|
| `app` | `homecenter` | **1, always** | Next.js + Prisma over a single SQLite file, everything under `/data` (`homecenter.db`, `uploads/`, `models/`, `search-index.db`, `backups/`). Exactly one replica: it is the only writer of the database file. Updates are `stop-first`. |
| `backup` | `homecenter` (same digest, no extra pull, a different entrypoint) | 1 | Runs HomeCenter's **own** backup engine (`npx tsx scripts/backup.ts`, the same code behind Settings → Data → "Back up now") on a schedule into `BACKUP_PATH`, a volume separate from the live data. `BACKUP_ENABLED: false` removes it. |

Traefik (skfenceha, or skfence) routes `https://<host>/` to `app:3000`
(health check `/api/health`). Networks: `skhome-<env>` (app, backup) and
`cloud-public-<env>` (app only).

### Why a real backup engine, not a file copy

HomeCenter's database is SQLite. Copying `homecenter.db` off a live,
mid-write file risks a torn copy. This stack therefore never copies the
raw file: the `backup` sidecar runs HomeCenter's own backup CLI
(`scripts/backup.ts`), the identical engine the app uses for its in-app
"Back up now" and scheduled-backup features, which is built to run safely
with the app up. Every run is then checked with `scripts/backup-verify.ts`
before being trusted; a failed backup or a failed verification is logged
and the previous archives are left untouched. Each archive holds the
database and every uploaded file as one file.

The sidecar needs **read-write** access to `/data` (not read-only, unlike
some other stacks' backup sidecars): HomeCenter's backup engine stages a
scratch copy under `<DATA_PATH>/backup-work`, on the same disk as the live
data, by upstream design.

### Placement and data

- `DATA_PATH` (default `/var/data/skhome-<env>/data`, NFS on a typical
  instance) is bind-mounted at `/data`. For local disk, set `DATA_PATH`
  (and `BACKUP_PATH`) outside `/var/data` and `DATA_NODE` to the node that
  holds it: `app` and `backup` get `node.hostname == <DATA_NODE>`, and the
  play refuses a local path without `DATA_NODE` (local data on a floating
  service is an empty instance after the next reschedule). Same rule as
  sksso/skhub/skpayment/skgym.
- `BACKUP_PATH` (default `/var/data/skhome-<env>/backup`) must be a
  **different** volume/disk than `DATA_PATH` where practical (same
  guidance HomeCenter's own docs give): a backup next to the data it is
  backing up does not survive that disk failing.
- `PLACEMENT_CONSTRAINTS` (app and backup) defaults to
  `["node.role == worker"]`.

### Secrets vs settings

The only secret is `AUTH_SECRET`: it signs every session cookie and
encrypts any mail-server password or AI provider key an admin saves in
Settings. The deploy writes it into the app's env file (the vault is the
source of truth, not a value HomeCenter generates itself), and fails
loudly when it is missing or shorter than 32 characters. **Changing it
signs everyone out and makes any already-stored mail/AI secret unreadable**
(HomeCenter detects this and asks for the value again rather than silently
failing). The `backup` sidecar also gets `AUTH_SECRET` (and `DATABASE_URL`)
so a scheduled backup keeps working even if an admin later turns on
password or recovery-key backup protection in Settings → Data; with the
default protection (`none`), neither value is actually read by the backup
engine.

### Sign-in and the first admin account

HomeCenter has its **own accounts** (email/password, admin or standard
user) and, per its own docs, **no OIDC/OAuth support** ("Private by
design": local login only, no third-party sign-in). Authentik therefore
cannot be its identity provider; it can only stand in front of it, same
two-path pattern as skgym/skpulse/skmesh: `AUTH_MODE`:

- **`sso` (default): Authentik forward auth in front.** `SSO_MIDDLEWARE`
  (default `authentik@file`, skfenceha's forward-auth middleware to the
  sksso embedded outpost) guards the whole router. Users sign in twice
  (Authentik, then their HomeCenter email/password); the gain is that
  nobody without an Authentik account ever reaches the login form, which
  matters for this app more than most: it can hold family health records.
  Authentik needs a proxy provider in forward-auth mode covering the
  skhome host, bound to the embedded outpost.
- **`local`: no SSO.** HomeCenter's own accounts only. For a private
  instance, add an IP allowlist to `TRAEFIK_MIDDLEWARES` as well.

Guard (the deploy refuses, naming the keys): an `AUTH_MODE` other than
`sso`/`local`; `AUTH_MODE: local` with `SSO_MIDDLEWARE` set; a forward-auth
middleware inside `TRAEFIK_MIDDLEWARES` (in `local` mode that is SSO under
another name; in either mode, use `AUTH_MODE: sso` / `SSO_MIDDLEWARE`
instead).

**First admin account.** `ADMIN_NAME` / `ADMIN_EMAIL` / `ADMIN_PASSWORD`
are read **once**, on an empty database, to seed the first account; the
image ignores them forever after that (there is no "SIGNUP" style runtime
re-check like skgym — HomeCenter does this entirely on first boot).
Changing them in the vault later has **no effect** on an existing
instance and the deploy does not warn about it (unlike skgym's profile
count, HomeCenter's own `db:seed` step owns this and the play cannot see
whether the seed has already run without opening the database). To change
the admin password after first boot, log in and use the app's own account
settings, or an admin invites/creates more users under Settings → Users.
If `ADMIN_PASSWORD` is left unset, the seeded password is `changeme` and
the user must change it on first login.

### Traefik

`TRAEFIK_MIDDLEWARES` is the router's whole middleware chain, default
`default-no-error-pages@file` (skfenceha: rate limit, security headers,
gzip) **not** the usual `default@file`/`default-no-crowdsec@file`: those
include skfenceha's `error-pages` middleware, which replaces every 400 and
402-599 response body with an HTML page, and HomeCenter's own API routes
(login, receipt parsing, the assistant) return JSON errors that the app's
own UI needs to read. On an instance running sksec, set
`TRAEFIK_MIDDLEWARES: "crowdsec-bouncer@file,default-no-error-pages@file"`.
`TLS_OPTIONS` defaults to `intermediate@file`; `none` omits the label.

### Health checks

`app`: `wget -qO- http://127.0.0.1:3000/api/health` (the same endpoint the
upstream image's own `HEALTHCHECK` already probes; this stack re-declares
it on the framework's schedule). `start_period` is tied to
`first_install_wait_seconds` (see below): `backup` has its health check
disabled (the image's own would probe an app it does not run on `:3000`).
Traefik's service health check is `/api/health`.

### First-start wait (`first_install_wait_seconds`)

A Docker healthcheck's `start_period` must cover the slowest legitimate
first start, or Swarm kills the task before it finishes and tries again
from zero (the same class of bug fixed for skhub, #213). HomeCenter's
first boot runs a staged-restore check, `prisma migrate deploy`, and a
database seed before the server starts; normally fast, but can be slower
on a loaded or NFS-backed node. `skhome.first_install_wait_seconds`
(default `120`) is **one knob for two places**: the compose healthcheck's
`start_period` for `app`, and the budget the deploy script's own
convergence wait polls against (`(first_install_wait_seconds + 4) / 5`
attempts, 5s apart) after `docker stack deploy`. Raise it for a
known-slow environment; the deploy announces the budget before waiting.

### Backups

The `backup` sidecar runs `npx tsx scripts/backup.ts -- --folder
<BACKUP_PATH>` (HomeCenter's own engine) then verifies the result with
`npx tsx scripts/backup-verify.ts <file>` before logging it as good. Two
schedules, same `backup_time`/`backup_tz` pattern as skhub/skbook:

- **`backup_time`** (`"HH:MM"`, 24-hour) + **`backup_tz`** (an IANA zone,
  default `UTC`): a fixed daily time. The deploy validates both (regex and
  a zoneinfo lookup on the control node) before writing them.
- Unset (default): a simple interval, `BACKUP_FREQUENCY` seconds (default
  `86400`) after an initial `BACKUP_START_DELAY` (default `60`).

Retention (how many backups to keep) is **HomeCenter's own**, read from
its Settings → Data (default `14` on a fresh instance with no settings
saved yet); the CLI has no `--keep` flag, so this role does not prune
independently. Raise or lower it from the app's own admin UI if needed.

With skbackup on the storage host (tier 2 copy of the app dir plus a
freshness check):

```yaml
skbackup:
  copy:
    apps:
      - name: skhome-prod
        newest: [{dir: backup, keep: 2}]
        dump: backup
```

**Restore** (manual, not automated by this role): stop the `app` service
(`docker service scale skhome-<env>_app=0`), then run the restore from a
one-off container against the same `DATA_PATH` and `BACKUP_PATH` mounts,
e.g.:

```bash
docker run --rm -v <DATA_PATH>:/data -v <BACKUP_PATH>:/backup \
  --entrypoint npx <image>@<digest> tsx scripts/restore.ts -- /backup/<file> --yes
```

then scale `app` back to 1 (the container's normal start-up finishes the
restore, same as upstream's own Docker restore instructions). Keep
`AUTH_SECRET` in the vault equal to the one the archive was made under, or
accept that the mail password and AI key need re-entering after restore
(unprotected archives) — protected archives (recovery key / password
zip) carry them regardless.

### Health data (sensitive)

Family health records are one of the opt-in features. This is exactly why
`AUTH_MODE: sso` is the default here rather than `local`: nobody without
an Authentik account ever sees the login form, let alone the health
section (which an admin can also hide entirely from inside the app,
Settings → Features). Treat the `admin` group binding the same as
skhub/skrac: restrict the Authentik application to the instance's admin
group, not every Authentik user, when health data is in scope.

### Email and AI assistant (both optional, both off by default)

- **Email**: `SMTP_HOST` (and `SMTP_PORT`/`SMTP_USER`/`SMTP_PASS`/
  `SMTP_SECURE`/`MAIL_FROM`) are optional vault keys; unset, no env block
  for them renders at all and HomeCenter never sends mail ("leave blank
  and no email is ever sent" — upstream). Settings saved in the app's own
  UI always win over these and survive even if the vault block is later
  removed.
- **AI assistant**: HomeCenter has **no environment variable** for the
  provider, base URL, model or API key; it is 100% admin-configured at
  runtime in **Settings → Assistant** (OpenAI, OpenRouter, Ollama, LM
  Studio, or any OpenAI-compatible server), and the key is stored
  encrypted in the database, not the vault. There is therefore no vault
  knob to "point it at the cluster's skgateway": that is a **post-deploy,
  in-app admin step**, exactly as optional as the feature itself. If an
  admin does want the cluster's skgateway (its OpenAI-compatible endpoint)
  as the assistant's provider, set it there, same as pointing at any other
  OpenAI-compatible server. Document the chosen endpoint next to the
  instance's other runbook notes, not in the vault (there is nowhere in
  the vault that would do anything).

### Resources

Sized for a small homelab node, a little above skgym's api because of the
bundled embedding model and on-device OCR: app limit 1.00 CPU / 768M
(reservation 128M), backup 0.25 CPU / 256M (32M). Knobs:
`HOMECENTER_CPUS`, `HOMECENTER_MEMORY_LIMIT`, `HOMECENTER_MEMORY_RESERVATION`.

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skhome/deploy_skhome-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

The play: checks the vault (fails on a missing `AUTH_SECRET`, an image
without a digest, a local data path without `DATA_NODE`, an invalid
`backup_time`/`backup_tz`, contradictory sign-in settings), creates the
directories, installs the backup script and the env/compose files, ensures
the networks, deploys stack `skhome-<env>`, and restarts `app` when its
env file changed (bind mounts and env files do not change the service spec
by themselves) and `backup` when its script changed.

## Configuration (vault)

`skhome:` in `optional/group_vars/<env>/skhome-<env>-<domain dashed>-<cluster>_vault.yml`.

```yaml
skhome:
  AUTH_SECRET: "<openssl rand -base64 32>"   # required, secret, at least 32 characters
  CLUSTERNAME: cluster1                     # default: inventory cluster_name
  DOMAIN: example.com                       # default: inventory domain
  CLOUDFLARED: false                        # true: default host is under DOMAIN, not CLUSTERNAME.DOMAIN
  HOSTNAME: ""                              # public host; default skhome[-dev|-staging].<base domain>
  TRAEFIK_MIDDLEWARES: ""                   # default default-no-error-pages@file (keeps HomeCenter's JSON errors)
  AUTH_MODE: sso                            # sso (Authentik forward auth) | local
  SSO_MIDDLEWARE: ""                        # sso mode: default authentik@file; must be empty in local mode
  TLS_OPTIONS: ""                           # default intermediate@file; "none" = no tls.options label
  ADMIN_NAME: Admin                         # first boot only, ignored after
  ADMIN_EMAIL: admin@localhost              # first boot only, ignored after
  ADMIN_PASSWORD: changeme                  # first boot only, ignored after; change it in-app afterwards
  MAX_UPLOAD_BYTES: 26214400
  NOTIFY_INTERVAL_MINUTES: 360              # 0 disables the reminder/digest timer
  DIGEST_HOUR: 8                            # 0-23
  DOCUMENT_REINDEX_HOURS: 6                 # 0 disables the timer (new uploads still index immediately)
  EMBEDDING_IDLE_MINUTES: 10
  SEARCH_INDEX_PATH: ""                     # default next to the database
  # SMTP_HOST: ""                           # unset = no email ever sent
  # SMTP_PORT: 587
  # SMTP_USER: ""
  # SMTP_PASS: ""                           # secret
  # SMTP_SECURE: true
  # MAIL_FROM: "HomeCenter <no-reply@homecenter.local>"
  # MAIL_DRY_RUN: false
  TZ: UTC
  extra_env: {}                             # any other upstream app variable, verbatim into skhome.env
  HOMECENTER_IMAGE: ""                      # default below; must carry @sha256:
  DATA_PATH: ""                             # default /var/data/skhome-<env>/data
  BACKUP_PATH: ""                           # default /var/data/skhome-<env>/backup (different disk where practical)
  DATA_NODE: ""                             # pin app + backup to this node (required with a local path)
  PLACEMENT_CONSTRAINTS: []                 # app + backup, default ["node.role == worker"]
  HEALTH_CHECK_INTERVAL: 30s
  HEALTH_CHECK_TIMEOUT: 10s
  HEALTH_CHECK_RETRIES: 3
  first_install_wait_seconds: 120           # app healthcheck start_period AND the deploy's convergence wait
  HOMECENTER_CPUS: "1.00"
  HOMECENTER_MEMORY_LIMIT: 768M
  HOMECENTER_MEMORY_RESERVATION: 128M
  BACKUP_ENABLED: true
  backup_time: ""                           # "HH:MM" 24-hour; unset = BACKUP_FREQUENCY interval instead
  backup_tz: UTC                            # only checked when backup_time is set
  BACKUP_FREQUENCY: 86400                   # seconds between backups (ignored if backup_time is set)
  BACKUP_START_DELAY: 60                    # seconds before the first backup (ignored if backup_time is set)
```

Default image: `ghcr.io/yatzin/homecenter:2.3.1@sha256:45bc4d6a0fed858ac...`
(full digest in the playbooks). Networks: override with the flat per-env
`skhome_<env>_networks` list.

## Files on the manager

| Path | Mode |
|---|---|
| `/var/data/config/skhome-<env>/skhome.env` | `0600` |
| `/var/data/config/skhome-<env>/skhome.sh.env` | `0640` |
| `/var/data/config/skhome-<env>/skhome.yml` (+ `docker-compose.yml` link) | `0644` |
| `/var/data/skhome-<env>/deploy` (+ `/var/data/deploy_skhome-<env>` link) | `0755` |
| `/var/data/skhome-<env>/bin/skhome-backup` | `0755` |
| `<DATA_PATH>/` and `<BACKUP_PATH>/` | `0700` root (HomeCenter chowns `/data` to uid 1001 on its own first start) |

## Networks

| Network | Subnet |
|---|---|
| `skhome-prod` / `-staging` / `-dev` | `172.16.175.0/24` / `172.16.176.0/24` / `172.16.177.0/24` |
| `cloud-public-<env>` | framework-shared Traefik network |

## Upgrades

Bump `HOMECENTER_IMAGE` to the new upstream release (tag and digest), read
the upstream README's "Updating" section, and redeploy; HomeCenter runs its
own migration on boot. A downgrade is a restore from a backup made under
the older version, not an image swap. The framework default moves only
through a framework release (digests checked by
`v1/scripts/check_images.sh`).

## Verification

```bash
docker service ls --filter name=skhome
curl -s https://<host>/api/health
ls -l <BACKUP_PATH>                    # a homecenter-*.hcbackup (or similar) file, newer than the last schedule
```

Tests: `v1/tests/test_skhome_render.py` (the deploy through real Ansible:
defaults, every knob set, knob-off paths, the missing-secret and
unpinned-image failures, the AUTH_MODE/backup_time/backup_tz guards) and
`v1/tests/test_skhome_backup.py` (the backup script's scheduling and
verification logic against a faked `tsx`). The skstack06 harness stage
`skhome` deploys it on the lab cluster in both `sso` and `local` modes and
checks: unauthenticated → Authentik redirect (sso mode), the app reachable
after login, data persisting across an `app` service restart, a backup
producing a restorable file, and the contradictory-config guard refusing a
bad vault.
