# SKWhoami

A trivial HTTP echo/test service (`traefik/whoami`), for verifying Traefik
routing, TLS termination and Docker Swarm service discovery on a fresh
`skstacks` instance.

## Overview

Three independent instances (`whoami01`, `whoami02`, `whoami03`) are
deployed, each with its own hostname and Traefik router, so a fresh cluster
can be smoke-tested end to end (DNS, TLS, ingress, service discovery)
without depending on any real application being up yet. There is no shared
state between instances and no data volume: `whoami` only echoes the
request it received.

Unlike the estate-specific version of this role, instances are not pinned to
named nodes: the framework does not assume any particular node-labeling
scheme, so Swarm's default scheduler places all three.

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skwhoami/deploy_skwhoami-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

Swap `prod` for `staging` or `dev` (and the matching playbook/vault
password/inventory) for the other environments.

## Configuration (vault)

```yaml
skwhoami:
  CLUSTERNAME: "skstack01"
  DOMAIN: "example.com"
  whoami_version: "v1.12.0"   # pinned tag; never :latest
```

## Networks

| Network | Purpose |
|---|---|
| `cloud-public-<env>` | Traefik ingress (framework-shared subnet) |
| `skwhoami-<env>` | Service-local overlay, allocated from the reserved `172.16.250.0/24`-`172.16.254.0/24` band |

## Verification

```bash
docker service ls | grep skwhoami
curl -s https://skwhoami01.<cluster>.<domain>
curl -s https://skwhoami02.<cluster>.<domain>
curl -s https://skwhoami03.<cluster>.<domain>
```
