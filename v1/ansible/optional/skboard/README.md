# skboard (Vikunja)

A self-hosted Kanban/task board (`vikunja/vikunja`) on Docker Swarm, single
container, sqlite database, no standalone DB service. Deployed the same way
as `skgraph`, `skpeek` and `skmail`: `deploy_skboard-{dev,staging,prod}.yml`
render a compose file and a `deploy` script onto the selected manager node,
which then runs `docker stack deploy`.

## Architecture

Vikunja's frontend and API ship as a single image. This role runs one
`vikunja` service with `VIKUNJA_DATABASE_TYPE=sqlite`, so there is no
Postgres/MariaDB sidecar and no separate backup service to configure.

The container's `/app/vikunja` directory holds both the sqlite database file
and the `files/` upload directory, so the role bind-mounts a single host
path onto it: `/var/data/skboard-<env>/data:/app/vikunja`. There is
intentionally only one volume, matching the live reference deployment's
layout; do not split the database and files onto separate mounts without
also updating `VIKUNJA_DATABASE_PATH` / `VIKUNJA_FILES_BASEPATH`.

## Required vault keys

None of the following has a built-in default; the playbook fails closed
(undefined-variable error) if they are missing, rather than silently using
a placeholder domain or a shared secret.

```yaml
skboard:
  CLUSTERNAME: "<this instance's cluster name>"
  DOMAIN: "example.com"
  JWT_SECRET: "<a long random string, stable across redeploys>"
```

`JWT_SECRET` matters more here than it looks: Vikunja auto-generates a JWT
signing secret at boot if `VIKUNJA_SERVICE_JWTSECRET` is unset, which
invalidates every logged-in session on every container restart or
reschedule. Setting it explicitly in the vault is what makes sessions
survive a redeploy.

## Optional vault keys

```yaml
skboard:
  VIKUNJA_VERSION: "0.24.6"        # default: 0.24.6 - see "Upgrading Vikunja" below
  INSTANCE: "primary"               # default: primary
  FILES_MAXSIZE: "20MB"             # default: 20MB
  LOG_LEVEL: "INFO"                 # default: INFO
  ENABLEREGISTRATION: "true"        # default: true
  TIMEZONE: "America/New_York"      # default: UTC
  WEEK_START: "1"                   # default: 0 (Sunday); 1 = Monday

  # Outbound mail (skip this block to leave mail disabled, the live
  # reference deployment's own configuration)
  MAILER_ENABLED: "true"
  MAILER_HOST: "smtp.your-relay.example"
  MAILER_PORT: "587"
  MAILER_USERNAME: "vikunja@example.com"
  MAILER_PASSWORD: "<mailer password>"
  MAILER_FROMEMAIL: "vikunja@example.com"

  # Deploy tuning (all optional; defaults shown)
  RESOURCES_LIMITS_CPUS: "0.50"
  RESOURCES_LIMITS_MEMORY: "512M"
  RESOURCES_RESERVATIONS_CPUS: "0.10"
  RESOURCES_RESERVATIONS_MEMORY: "128M"
  RESTART_POLICY_CONDITION: "any"
  RESTART_POLICY_DELAY: "10s"
```

`MAILER_ENABLED` only toggles the `VIKUNJA_MAILER_ENABLED` env var; the
actual SMTP block (`MAILER_HOST`/`MAILER_USERNAME`/`MAILER_PASSWORD`/...) is
only rendered into `skboard.env` when `MAILER_HOST` is set, so an instance
that never sets it gets no dangling, empty mailer config.

## Restart policy

`vikunja` restarts on any exit (`condition: any`, 10s delay) with no
`max_attempts` and no `window`. Until skstacks v2.22.0 the default was
`on-failure` with 3 attempts in a 120s window, copied from the reference
deployment, and that deployment's vikunja then sat at 0/1 for about four
months: the service is pinned to a manager, an outage of the node it ran on
spent the whole budget, and Swarm never scheduled it again. A restart
budget is lifetime state per task slot, not a crash-loop brake, so the
framework no longer sets one on any long-running service
(`v1/tests/test_restart_policy_never_gives_up.py`).

`RESTART_POLICY_MAX_ATTEMPTS` and `RESTART_POLICY_WINDOW` are gone. A vault
that still sets either one stops the deploy with a message naming them:
delete them.

## Upgrading Vikunja

`skboard.VIKUNJA_VERSION` pins the exact image tag; the framework default
(`0.24.6`) is only what a brand-new instance gets if it never sets this key,
not a signal that a running instance should move to it. Vikunja has shipped
newer `0.24.x` point releases and a `1.x` line upstream; this role does not
track either automatically. Before bumping `VIKUNJA_VERSION`, check the
currently running image (`docker service inspect --format
'{{.Spec.TaskTemplate.ContainerSpec.Image}}' <stack>_vikunja`) and read the
[Vikunja release notes](https://github.com/vikunja/vikunja/releases) between
what you run and what you're moving to, especially across the `0.24` to
`1.x` boundary.

## Networking

`skboard` gets its own dedicated overlay network (`skboard-<env>`), matching
every other single-container optional service in this framework
(`skgraph`, `skpeek`, `skgit`, ...) rather than relying on Swarm's
stack-default internal network. It also attaches to `cloud-public-<env>`
for Traefik routing. The live reference deployment's own internal network
(`skboard-prod_skboard-internal`, a Swarm stack default) was never given a
registered subnet because it was deployed ad hoc outside this framework;
this role brings it in line with the framework's convention of a dedicated,
registered `172.16.x.0/24` subnet per service per environment.
