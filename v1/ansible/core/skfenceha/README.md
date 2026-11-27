# skfenceha

HA (multi-manager) Traefik edge: one ACME-master Traefik (certificate renewal
only, on the manager labeled `traefik.acme.master=true`) plus a `global`-mode
worker tier that does all real routing, with the same error-pages and
certs-dumper sidecars as skfence. **Do not run skfence and skfenceha on the
same cluster.**

## Which manager is the ACME master

The deploy keeps the `traefik.acme.master` label where it is: when exactly one
manager in the target group carries it and that node is Ready and Active, it
stays there. It moves to the manager this run selected only when no manager has
it, more than one has it, or the holder is down or drained. Set
`skfenceha.ACME_MASTER_NODE: <manager>` in the vault to pin it (the deploy fails
if that name is not in the target manager group). skha renders its keepalived
priorities from the same label (ACME master 150), so after the label moves, run
the skha deploy too. Check where it is with
`docker node ls -q | xargs docker node inspect --format '{{.Description.Hostname}} {{index .Spec.Labels "traefik.acme.master"}}'`.

Vault vars, TLS modes, the dashboard host override and access-log header
redaction are documented in `v1/ansible/core/skfence/README.md`; the
published-service catalog entry is in `docs/v1-service-catalog.md`.

**Dashboard protection (`DASHBOARD_AUTH_MODE`)**: the Traefik dashboard and API
(`/dashboard/`, `/api`) always carry exactly one gate, chosen by
`DASHBOARD_AUTH_MODE`:

1. `sso` (the default): the Authentik forward-auth middleware
   (`DASHBOARD_SSO_MIDDLEWARE`, default `authentik@file`) and nothing else.
   No basic-auth middleware and no htpasswd hash are rendered, so the
   dashboard portal tile signs you in through SSO instead of landing on a
   browser basic-auth prompt. The domain-wide forward-auth provider on the
   embedded outpost covers the dashboard host like any other service host
   (bind the group that may see it in Authentik). Needs sksso.
2. `basic`: basic auth from `DASHBOARD_AUTH_HASH` (`htpasswd -nbB user
   password`, or `src/skfenceha/create_user_password.sh`), realm
   `DASHBOARD_AUTH_REALM`. Explicit opt-in for an estate without sksso. With
   no hash it falls back to the `lan` behaviour.
3. `lan`: an ipAllowList admits `DASHBOARD_FALLBACK_ALLOW_CIDRS` (default
   loopback and the RFC 1918 ranges; add a tailnet or VPN range here to reach
   the dashboard over it) and answers 403 to everyone else.

Until 2026-11-14 the default depended on which variables were set: a hash meant
basic auth even when sksso was deployed, which put a 401 in front of the SSO
tile. Set `DASHBOARD_AUTH_MODE: basic` to keep the old behaviour on an estate
that has no sksso, and drop `DASHBOARD_AUTH_HASH` from the vault once the mode
is `sso` (it is ignored there). Until 2026-11-09 the no-gate case rendered an
empty chain, so an estate that never set the hash served its dashboard and API
to the internet.

These are dynamic configuration: a redeploy re-renders them and the workers
reload the files without a restart. Traefik reads the dynamic directories
recursively, hidden directories included, and the first definition of a name
wins, so a leftover `.bak-*` directory inside `dynamic/` or `dynamic_acme/`
shadows every rendered file: the deploy reports changed and the edge keeps
serving the old config. The play refuses to render while one exists (keep
backups under `/var/data/backups`; `skfenceha.ALLOW_DYNAMIC_SUBDIRS: true`
opts out). On shared NFS a write from one manager does not wake the file watch
on the other managers: after a dynamic-only change, `touch` the changed files
on each manager.

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

**Worker resources (`TRAEFIK_WORKER_CPUS` and friends)**: the `traefik-worker`
tier carries every request and TLS handshake of the cluster, and a hard 0.40
CPU limit capped large transfers (a 500 MB WebDAV download held the worker at
the limit and ran near 15 MB/s). Defaults: `TRAEFIK_WORKER_CPUS: "2.00"`,
`TRAEFIK_WORKER_MEMORY_LIMIT: 384M`, `TRAEFIK_WORKER_CPUS_RESERVATION: "0.08"`,
`TRAEFIK_WORKER_MEMORY_RESERVATION: 96M`. The limit applies per manager (the
service is global). A deploy that changes it restarts each worker in turn
(host-mode ports, so expect a brief connection reset per node); a live
`docker service update --limit-cpu` does the same.

**Mail entrypoints (`MAIL_ENTRYPOINTS`)**: `skfenceha.MAIL_ENTRYPOINTS: true`
adds plain TCP entrypoints `smtp` (25), `submissions` (465), `submission`
(587), `imap` (143) and `imaps` (993) to every worker, published in host mode
like 80/443, and a `skmail-proxy-v2` TCP serversTransport (PROXY protocol v2)
to `dynamic/services.yml`. They exist for skmail with `skmail.VIA_TRAEFIK`,
which routes its mail ports through them so mail answers on every worker. The
skmail README ("Behind Traefik (PROXY protocol)") has the deploy order: skmail
first, because a worker cannot bind a port skmail still publishes in host
mode on the same node. Static configuration: the deploy restarts each worker
in turn. Default `false`: nothing renders differently.

**NFS and port 993.** The kernel NFS client takes its source ports from
`sunrpc.min_resvport`..`max_resvport` (665-1023), and 993 is inside that
range. If an NFS mount (`/var/data`) connects first after a reboot, it can own
993, and the worker on that node then loops on "bind 0.0.0.0:993: address
already in use" (no process shows, because it is a kernel socket). The deploy
therefore runs `shared/tasks/nfs_resvport_guard.yml` on every manager. It sets
`max_resvport` just below the lowest published port inside the range (992 with
`MAIL_ENTRYPOINTS`) in `/etc/modprobe.d/sunrpc.conf` (the setting that holds at
boot, since sunrpc loads after systemd-sysctl) and in
`/etc/sysctl.d/96-sunrpc-resvport.conf`, and sets the live value. A socket
already holding the port is reported; remount the share or reboot that node
to free it. Run it alone with `--tags sunrpc`.

**Traefik image (`TRAEFIK_IMAGE`)**: `skfenceha.TRAEFIK_IMAGE` pins the
image for both `traefik-acme` and `traefik-worker` (they move together;
default `traefik:v3.7.19@sha256:...`). A separate `skfence.TRAEFIK_IMAGE`
pins skfence's own single `traefik` service -- the two are different vault
keys under different top-level dicts, not one shared knob, since an
instance can run only one of skfence or skfenceha at a time but the
framework default for each is tracked independently. Pin a digest to stage
a tactical version ahead of or instead of a full framework release.

**certs-dumper image (`DUMPER_IMAGE`)**: same convention, pins
`traefik-certs-dumper`'s image (default
`ldez/traefik-certs-dumper:v2.11.4@sha256:...`; v2.10.0 panics
("unsupported key type: ''") on acme.json written by Traefik 3.7+, since
the account KeyType field was dropped -- see CHANGELOG). `skfence.DUMPER_IMAGE`
is the same knob for skfence's own dumper, independent of this one.

**Cert reload without a restart (`traefik-certs-watch`)**: `traefik-worker`
reads `acme.json` once, at container start, and never again -- a cert renewed
or newly issued by `traefik-acme` never reached a running worker without
restarting it (confirmed on the NAM estate, 2026-10-24; a worker cert expired
on one estate because nothing ever restarted the workers after a renewal).
Fixed by `traefik-certs-watch`: a tiny sidecar (global mode, same placement
as `traefik-worker`, no Docker socket, no network) that runs on every
manager node, polls the shared, read-only `/certs` directory
`traefik-certs-dumper` already writes (previously mounted into
`traefik-worker` but never used), and -- only when the result actually
changes -- (re)writes a `tls.certificates` file (`certs-generated.yml`) into
the same shared `dynamic/` directory `traefik-worker`'s file provider
already watches (`watch: true`). Traefik's own file-provider watch has no
polling fallback and relies on local fsnotify, which a write from a
*different* node never reaches over NFS (the dynamic config directory is
shared NFS too, which is also why a change rendered by Ansible on one
manager needs confirming on every manager before you rely on it); running
this sidecar on every node sidesteps that instead of fighting it, because
each node's own write to the shared file is always a *local* write as far
as that same node's own Traefik watch is concerned. `skfenceha.CERTS_WATCH_IMAGE` and
`skfenceha.CERTS_WATCH_INTERVAL_SECONDS` (default 15) override the sidecar's
image and poll interval; `certs-generated.yml` is a framework-reserved
dynamic file name (an instance's `EXTRA_DYNAMIC_FILES` cannot use it).

**Strict SNI (`DEFAULT_SNI_STRICT`)**: routers use the `default` TLS options in
`tls.yml`. `skfenceha.DEFAULT_SNI_STRICT: true` makes `default` refuse a handshake whose
SNI matches no certificate; unset (`false`), that host is served Traefik's self-signed
default cert. Same knob as skfence.

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
