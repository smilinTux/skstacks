# SKStacks v1 (Docker Swarm + Ansible)

v1 is the Swarm framework the current production clusters run on. It is being
published one service at a time as each is cleaned of estate values; today
this tree holds **skgraph** (FalkorDB) and the shared tasks it uses.

## Instance contract

An instance repo pins this framework as `framework/` and supplies:

- an inventory (`-i`) whose hosts carry `domain` and `cluster_name`, and a
  manager group you pass as `-e target_manager_group=<group>`;
- `skstacks_vault_dir`, pointing at the instance's vault tree. A service's
  vault is looked up at
  `<skstacks_vault_dir>/<core|optional|standalone>/group_vars/<env>/<service>-<env>-<domain with dashes>-<cluster>_vault.yml`,
  falling back to `<service>-<env>_vault.yml` in the same directory;
- the vault password via `--vault-password-file`.

Example:

    ansible-playbook -i envs/dev/inventory.ini \
      framework/v1/ansible/optional/skgraph/deploy_skgraph-dev.yml \
      -e target_manager_group=swarm_managers \
      --vault-password-file ~/.vault_pass_env/.<instance>_dev_vault_pass

Tests: `python3 -m pytest -q v1/tests`.
