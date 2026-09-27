# SKPeek

SearXNG metasearch (`searxng/searxng`, pinned by digest) with a Valkey sidecar
for the limiter/cache, published through Traefik at
`skpeek[-<env>].<cluster>.<domain>`. skseek (Perplexica) uses it as its search
backend and needs it deployed first in the same environment.

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skpeek/deploy_skpeek-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

Swap `prod` for `staging` or `dev` (and the matching playbook/vault
password/inventory) for the other environments.

## Configuration (vault)

```yaml
skpeek:
  CLUSTERNAME: "cluster1"
  DOMAIN: "example.com"
  SECRET_KEY: "<random, required>"   # SearXNG server.secret_key
  INSTANCE_NAME: "SKPeek"            # optional, settings.yml general.instance_name
  DEBUG: false                       # optional, settings.yml general.debug
  THEME: "simple"                    # optional, settings.yml ui.default_theme
  extra_env:                         # optional, appended to skpeek.env
    SEARXNG_PRIVACY: "1"
```

`CLUSTERNAME`/`DOMAIN` fall back to the inventory `cluster_name`/`domain`.

### Custom settings.yml: `skpeek.SETTINGS_YML`

By default the deploy renders `settings.yml` from the framework template
(`src/skpeek/etc/settings.yml.j2`, a full upstream-style file), where only the
keys above are hookable. An instance that runs its own settings (for example
a minimal file without `use_default_settings`, its own engine list, outgoing
timeouts, DNS resolvers or `redis.url`) sets `skpeek.SETTINGS_YML` to the
whole file as a string, and the deploy writes it **verbatim** instead:

```yaml
skpeek:
  CLUSTERNAME: "cluster1"
  DOMAIN: "example.com"
  SECRET_KEY: "<random>"
  SETTINGS_YML: !unsafe |
    general:
      instance_name: "SKPeek"
    server:
      secret_key: "<random>"
      limiter: false
    redis:
      url: valkey://redis:6379/0
    outgoing:
      request_timeout: 4.0
    engines:
      - name: duckduckgo
        engine: duckduckgo
        shortcut: ddg
```

- The value must be a string holding YAML that parses as a mapping. Anything
  else (invalid YAML, a scalar or list, or a YAML map instead of a `|` block
  string) stops the deploy with an error naming `skpeek.SETTINGS_YML`, before
  `settings.yml` is written. The error never prints the value.
- Tag the value `!unsafe` so Ansible writes it literally. Without it, any
  `{{ ... }}` or `{% ... %}` in the file (SearXNG's `command` engines use
  `{{QUERY}}`) is templated by Ansible first.
- None of the other `skpeek.*` settings keys (`INSTANCE_NAME`, `DEBUG`,
  `THEME`, `SECRET_KEY`) are applied to a custom file: it is the whole file.
  Set `server.secret_key` in it (or `SEARXNG_SECRET` via `extra_env`).
- The value can hold `server.secret_key`: keep it in the vault.
- Unset or empty: the framework template is rendered exactly as before.
- `limiter.toml` and `uwsgi.ini` are still rendered from the framework
  templates.

`settings.yml` is written `root:root` mode `0640` either way (it carries
`server.secret_key`, so it is not world-readable). The SearXNG entrypoint runs
as root and chowns `/etc/searxng` to `searxng:searxng`, so the container
still reads it.

## Files on the manager

| Path | Mode |
|---|---|
| `/var/data/skpeek-<env>/etc/settings.yml` | `0640` |
| `/var/data/skpeek-<env>/etc/uwsgi.ini`, `limiter.toml` | `0644` |
| `/var/data/config/skpeek-<env>/skpeek.env` | `0644` |
| `/var/data/config/skpeek-<env>/skpeek.sh.env` | `0640` |

## Networks

| Network | Purpose |
|---|---|
| `cloud-public-<env>` | Traefik ingress (framework-shared subnet) |
| `skpeek-<env>` | Service-local overlay (SearXNG to Valkey) |

Override with `skpeek_<env>_networks` in the vault.

## Verification

```bash
docker service ls --filter name=skpeek-<env>
curl -s -o /dev/null -w '%{http_code}\n' https://skpeek.<cluster>.<domain>/
```
