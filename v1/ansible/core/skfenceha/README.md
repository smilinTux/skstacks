# skfenceha

HA (multi-manager) Traefik edge: one ACME-master Traefik (certificate renewal
only, on the manager labeled `traefik.acme.master=true`) plus a `global`-mode
worker tier that does all real routing, with the same error-pages and
certs-dumper sidecars as skfence. **Do not run skfence and skfenceha on the
same cluster.**

Vault vars, TLS modes, the dashboard host override and access-log header
redaction are documented in `v1/ansible/core/skfence/README.md`; the
published-service catalog entry is in `docs/v1-service-catalog.md`.

**Behind a tunnel or CDN (`TRUSTED_PROXIES` + `DROP_FORWARDED_HEADERS`)**:
`TRUSTED_PROXIES` (list) renders `forwardedHeaders.trustedIPs` on the
worker's `web` and `websecure` entrypoints, so Traefik keeps the
`X-Forwarded-For` a tunnel connector or CDN sends (the real client; the
access log records it as `ClientHost`, which CrowdSec reads; see the sksec
README "Behind a tunnel or CDN"). Traefik then trusts every `X-Forwarded-*`
header from those sources, and an edge such as Cloudflare passes a
client-sent `X-Forwarded-Host` through unchanged: apps that build absolute
URLs from it (Authentik does) can be made to emit links to a forged host.
`DROP_FORWARDED_HEADERS: [X-Forwarded-Host]` removes those headers on the
`websecure` entrypoint before any router (a `drop-forwarded-headers@file`
entrypoint middleware); apps fall back to the `Host` header Traefik routed
on, and Traefik's forwardAuth still sends the real host. Both are static
configuration: restart the workers after changing them. Unset: nothing
renders differently.

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
