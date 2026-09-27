# skfence

Single-node Traefik edge: reverse proxy, TLS termination, a
`docker-socket-proxy` sidecar (Traefik never touches `docker.sock` directly),
`certs-dumper`, and branded error pages (see `src/error-pages/README.md`).
Every other service's Traefik labels assume one of skfence or skfenceha is
running.

**Prerequisites**: none. Deploy this (or skfenceha) first on a new cluster.

**Minimal vault snippet** (`core/group_vars/prod/skfence-prod-<domain>-<cluster>_vault.yml`):

```yaml
skfence:
  ACME_ENABLED: false            # true for real Let's Encrypt certs
  # CLOUDFLARE_EMAIL: "changeme@example.com"   # required only if ACME_ENABLED: true
  # CLUSTERNAME / DOMAIN fall back to the inventory's own cluster_name/domain
```

**Deploy**:

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/core/skfence/deploy_skfence-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

**Health check**: `curl http://<manager-ip>:8082/ping`; certs-dumper carries
its own container healthcheck.

**Gotchas**: single replica, so there is exactly one ACME lease per cluster.
Run skfenceha instead on a multi-manager cluster that needs HA. Rate limiting
is unconditional (always on) in skfence's own dynamic-middlewares template;
there is no `RATE_LIMIT_ENABLED` toggle here (that knob only exists on
skfenceha).

**Dashboard/API hostname override**: both skfence and skfenceha's dashboard
+ API router (and its two redirect routers) build
`<name>[-env].<cluster>.<domain>` by default, same as every other router in
the file. Set `skfence.DASHBOARD_HOST` (or `skfenceha.DASHBOARD_HOST`) to a
literal hostname to override just that router group - for an estate whose
dashboard is reachable at a bare `<name>.<domain>` (no cluster segment)
instead. Everything else (`catch-all`, the wildcard routers) keeps building
the normal computed hostname regardless. Default (unset): unchanged.

**skfenceha access-log header redaction**: skfenceha's worker-role Traefik
config (`traefik-worker.yml.j2`, the role that handles all real traffic)
renders a static `accessLog.fields.headers` block. `skfenceha.ACCESS_LOG_HEADERS`
(`{defaultMode: keep|drop|redact, names: {<Header>: keep|drop|redact, ...}}`)
overrides it entirely; unset, the framework default redacts `Authorization`,
`Cookie`, `Set-Cookie`, `X-Api-Key` and `Proxy-Authorization` (`defaultMode:
keep` for everything else) - a behaviour change from relying on Traefik's
own built-in default, see CHANGELOG. skfence has no equivalent (no
`log:`/`accessLog:` block is rendered at all for it).

**Instance-owned dynamic files (`EXTRA_DYNAMIC_FILES`)**: the playbooks render
a fixed set of Traefik dynamic files (`routers.yml`, `services.yml`,
`middlewares.yml`, `tls.yml`). An instance that needs routes of its own (for
example a hypervisor web UI that is not a Swarm service) lists extra
templates in `skfence.EXTRA_DYNAMIC_FILES` (or `skfenceha.EXTRA_DYNAMIC_FILES`);
they are rendered into the same `dynamic/` dir right after the fixed files,
in dev, staging and prod alike:

```yaml
skfenceha:
  EXTRA_DYNAMIC_FILES:
    - src: traefik/pve.yml.j2    # relative to the inventory dir, or absolute
      dest: pve.yml              # plain file name under dynamic/
      acme: true                 # skfenceha only: also render into dynamic_acme/
```

- `src` is an absolute path or a path relative to the instance's inventory
  directory (`inventory_dir`, e.g. `envs/prod/`). It is not resolved against
  the playbook's own directory, which is the framework checkout.
- `dest` must be a plain file name ending `.yml` or `.yaml` (no `/`), and may
  not shadow a framework-owned file (`routers.yml`, `services.yml`,
  `middlewares.yml`, `tls.yml`, `sksec.yml`). Every entry is checked before
  anything is written; a bad entry fails the run.
- `acme` (default false) only exists on skfenceha, where the ACME master
  reads `dynamic_acme/`; set it when the ACME master needs to see the router
  (for example to request its certificate). skfence rejects it.
- The templates are rendered with the playbook's variables, so they can use
  `{{ env }}`, `{{ domain }}` and the service's vault vars. Referencing
  framework middlewares (`default@file`) and services works as usual.
- Default (unset or empty): nothing is rendered, no change.
- Removing an entry does not delete the file it rendered; remove the file
  from `dynamic/` (and `dynamic_acme/`) by hand.

See `docs/v1-service-catalog.md` for the full published-service catalog.
