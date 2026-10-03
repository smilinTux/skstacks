# SKStacks v1 (Docker Swarm + Ansible)

v1 is the Swarm framework the current production clusters run on. It is being
published one service at a time as each is cleaned of estate values.

**Full service catalog**: [`docs/v1-service-catalog.md`](../docs/v1-service-catalog.md)
covers every published service (what it is, dependencies, exposed
ports/hostnames, storage, required vault vars, opt-in flags, deploy command,
health check and gotchas). This README only covers the framework-wide
instance contract.

## Published services

- **Core** (`v1/ansible/core/`, one per cluster): skfence, skfenceha (HA
  alternative to skfence), skha, sksec.
- **Optional** (`v1/ansible/optional/`, deploy the ones you want): skboard,
  skbook, skdash, skdesk, skform, skgallery, skgit, skgraph, skhub, skmail,
  skmem-pg, skmesh, skmon, skorch, skpdf, skpeek, skport, skpulse, skreg,
  skseek, sksso, skstor, sksync, skvector, skwhoami, skbackup.

See the catalog for the tier table and per-service detail; this list is kept
in sync with it.

## Instance contract

An instance repo pins this framework as `framework/` and supplies:

- an inventory (`-i`) whose hosts carry `domain` and `cluster_name`, and a
  manager group you pass as `-e target_manager_group=<group>`;
- `skstacks_vault_dir`, pointing at the instance's vault tree. A service's
  vault is looked up at
  `<skstacks_vault_dir>/<core|optional|standalone>/group_vars/<env>/<service>-<env>-<domain with dashes>-<cluster>_vault.yml`,
  falling back to `<service>-<env>_vault.yml` in the same directory;
- the vault password via `--vault-password-file`;
- **`/var/data` shared by every swarm node** (for example an NFS mount).
  v1 services write configs and data under `/var/data` on the manager that
  runs the playbook and bind-mount them into containers that may land on any
  node, so a node-local `/var/data` leaves tasks pending forever.

Example:

    ansible-playbook -i envs/dev/inventory.ini \
      framework/v1/ansible/optional/skgraph/deploy_skgraph-dev.yml \
      -e target_manager_group=swarm_managers \
      --vault-password-file ~/.vault_pass_env/.<instance>_dev_vault_pass

Tests: `python3 -m pytest -q v1/tests`.

## Operational tooling: `v1/scripts/` vs `tools/`

Two places hold CLI tooling for v1, split by what the tool needs:

- **`v1/scripts/`** -- tooling that only reads the framework tree itself
  (no instance, no live docker/ansible): `check_images.sh` + `list_images.py`
  walk the compose templates to list every referenced image. Tests live in
  `v1/tests` and CI already triggers on `v1/**`.
- **`tools/`** (framework root) -- tooling that drives an actual deploy
  against an instance repo and a live swarm/ansible controller, matching
  the sibling harness repo's own `tools/` layout
  (`skstack06/tools/live_parity.sh`, `tools/merge_pr_if_green.sh`):
  `skstacks-deploy` (one-command deploy with automatic rollback, see
  `tools/README.md`). Tests live beside the tool (`tools/test_*.py`); CI
  triggers on `tools/**`.

When in doubt: if it needs `--instance <config-repo>` or touches
docker/ansible for real, it goes in `tools/`; if it only reads this repo's
own files, it goes in `v1/scripts/`.
