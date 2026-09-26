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

See `docs/v1-service-catalog.md` for the full published-service catalog.
