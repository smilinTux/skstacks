# tools/skstacks-deploy

One-command SKStacks v1 (Docker Swarm) service deploy with automatic
rollback. Wraps the manual sequence real prod deploys already follow: lock,
backup, snapshot, record the current service specs, run the playbook,
verify, and restore only what broke.

```
tools/skstacks-deploy <service> --instance <config-repo-dir>
    [--execute] [--cluster] [--env ENV] [--lock-timeout SECONDS]
```

- `<service>`: a framework app name that has
  `v1/ansible/optional/<service>/probe.yml` (see below).
- `--instance DIR`: the private per-site config repo checkout you are
  deploying from (vault, inventory, `skstacks-deploy.yml` all live there).
- No `--execute`: dry run. Prints the full plan and runs **zero**
  subprocesses, not even a read-only `docker service inspect`. This is the
  default on purpose.
- `--execute`: runs it for real, under a lock (`sk-lock <service>` by
  default, `sk-lock --cluster` with `--cluster` for multi-service/heavy-NFS
  work).
- `--env`: overrides the instance's `env` setting (default `prod`).

Exit codes: `0` success. `75` lock contention -- nothing ran, matching
`sk-lock`'s own contract. `1` deploy failed, auto-restore brought the
affected service(s) back to their recorded spec. `2` deploy failed,
auto-restore also failed -- needs a human, evidence is in the run
directory. `3` deploy failed and auto-restore does not apply (the app is
`schema_changing`, runs outside swarm, or a backup/hook failed before the
playbook ever ran) -- the manual restore steps are printed, nothing beyond
the backup/record steps was touched.

## Instance settings: `<instance>/skstacks-deploy.yml`

Plaintext, not a vault file -- it only names commands and paths, never a
secret. Every key is optional.

```yaml
env: prod
manager_group: skstack01-douno-managers
vault_password_file: ~/.vault_pass_env/.prod_vault_pass
inventory: v1/ansible/shared/hosts   # relative to the instance dir
lock_timeout: 14400
snapshot_hook: "ssh root@<nas-host> sk-predeploy-snap {purpose}"
allow_no_snapshot_hook: false        # true opts OUT of the fail-closed check below
services:
  skhub:
    base_url: https://skhub.example.test
    db_dump_hook: "ssh <db-host> '...' | gzip -1 > {backup_dir}/skhub-all.sql.gz"
    parity_hook: "tools/live_parity.sh skhub ."
```

| Key | Scope | Meaning |
|---|---|---|
| `env` | instance | Default environment (`dev`/`staging`/`prod`); `--env` overrides it. |
| `manager_group` | instance | Ansible `-e target_manager_group=... -l ...`. Omitted: the playbook runs with no group restriction. |
| `vault_password_file` | instance | Passed to `ansible-playbook --vault-password-file`. `~` is expanded. Omitted: no vault flag is passed. |
| `inventory` | instance | Path to the Ansible inventory, relative to the instance dir. Default `v1/ansible/shared/hosts`. |
| `lock_timeout` | instance | Seconds passed to `sk-lock -w`. Default 14400 (4h). `--lock-timeout` overrides it. |
| `snapshot_hook` | instance | Shell command run before every deploy, with `{purpose}` substituted (`<service>-<UTC timestamp>`, sanitized to `[a-z0-9-]` -- `sk-predeploy-snap` rejects anything else). **Missing and `allow_no_snapshot_hook` is not `true` aborts the deploy before the lock does anything** (fail closed: no snapshot hook configured usually means nobody wired up a real backup path yet). A hook that exits non-zero also aborts, before the record/playbook steps run. |
| `allow_no_snapshot_hook` | instance | Opt an instance out of the fail-closed check above (e.g. a stateless service with nothing to snapshot). |
| `services.<app>.base_url` | per-service | Base URL `probe.yml`'s `http_checks` are run against. Required if that app declares any `http_checks`; missing it fails the whole app's verify. |
| `services.<app>.db_dump_hook` | per-service | Shell command run before `snapshot_hook`, with `{backup_dir}` (a fresh directory this tool creates under `<instance>/.skstacks-deploy/runs/<ts>/backup/`), `{service}`, `{env}` substituted. The hook decides what to write there and under what name(s); a non-zero exit aborts the deploy the same as a failed `snapshot_hook`. |
| `services.<app>.parity_hook` | per-service | Shell command run as part of verify, with `{service}`, `{env}`, `{instance}` substituted (e.g. `tools/live_parity.sh <service> .` from `skstack06`). A non-zero exit marks the whole app failing. |

## Per-app probe file: `v1/ansible/optional/<app>/probe.yml`

Declarative, framework-side (not instance-side): it says what the app's
stack looks like and how risky an auto-restore is, independent of which
site is deploying it.

```yaml
services: [nextcloud, db, db-backup, redis, clamav, cron, notify_push,
           whiteboard, imaginary, collabora, talk-hpb]
mode: swarm
schema_changing: true
http_checks:
  - name: status.php
    path: /status.php
    method: GET
    expect_status: 200
  - name: webdav-propfind
    path: /remote.php/dav/
    method: PROPFIND
    expect_status: 207
```

| Key | Required | Meaning |
|---|---|---|
| `services` | yes | Compose service names (unqualified) in this app's stack, namespaced at runtime as `<service>-<env>_<name>`. Every one gets its `docker service inspect` spec recorded before the playbook runs (swarm mode only -- see `mode`). |
| `mode` | yes | `swarm` (deployed with `docker stack deploy` / `community.docker.docker_swarm_service_info`) or `compose` (a host-level `docker compose` project under a systemd unit, like skfetch). Only `swarm` services can be auto-restored (`docker service update --rollback`); `compose` always takes the manual-restore-steps path on a failed verify, the same as `schema_changing`. |
| `schema_changing` | yes | `true` if a redeploy can run an irreversible data/schema migration (a Nextcloud `occ` upgrade, Authentik migrations, a Laravel migration). Such an app is never auto-restored on a failed verify, regardless of `mode`: the recorded-spec paths and the manual `docker service update --rollback` commands are printed and the tool exits non-zero instead. |
| `http_checks` | no (default `[]`) | Checked after the playbook, against `"<base_url><path>"` where `base_url` comes from the instance's `services.<app>.base_url`. Every entry must pass (status code match) for the deploy to count healthy; a failing check (or a missing `base_url` when checks are declared) marks the **whole app** failing, since an HTTP-level failure can't be attributed to one docker service. Fields: `name`, `path`, `method` (default `GET`), `expect_status`. |

### Known gaps (documented, not silently dropped)

- **skstream**: the real verification execs inside the `plex` container
  (`docker exec ... curl localhost:32400/identity`); a plain
  `base_url + path` HTTP check can't express that, so `skstream/probe.yml`
  ships with `http_checks: []`. Replica/running-state verification still
  applies. A future `remote_probe_hook`-style mechanism (run a declared
  command instead of/alongside an HTTP check) would close this.
- **skfetch**: `mode: compose`. `services` is recorded for visibility, but
  since there is no swarm service to inspect, `docker service inspect`
  simply reports it absent and nothing is restorable from this tool; a
  failed skfetch deploy always takes the manual-restore-steps path.

## Testing

`tools/test_skstacks_deploy.py` drives the tool against fake `docker`,
`ansible-playbook` and `sk-lock` scripts on `PATH` in a `tmp_path`, plus a
throwaway `probe.yml`/`skstacks-deploy.yml` tree (via
`SKSTACKS_DEPLOY_FRAMEWORK_ROOT`, test-only). Nothing it does ever reaches
a real docker daemon, ansible controller, or network beyond a loopback
`http.server` the test itself starts. Run with:

```
python -m pytest -q tools
```
