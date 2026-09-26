# SKPort (Portainer)

Portainer CE, a web UI for managing this Docker Swarm cluster.

## Overview

SKPort deploys Portainer Community Edition as a single-replica service
constrained to a manager node. It never mounts `/var/run/docker.sock`
directly: it reaches the Swarm API over `DOCKER_HOST` through whichever
socket-proxy service is already running (`skfence-<env>_socket-proxy` on a
single-node cluster, `skfenceha-<env>_socket-proxy` on an HA cluster). The
deploy playbook auto-detects which one is present at deploy time and has no
hard dependency on either name existing in advance; it falls back to
`skfence` if neither is found yet, matching that stack's default name.

There is no Portainer Agent service. Portainer's own recommended
distributed-agent model is unnecessary here because socket-proxy already
gives it cluster-wide visibility without an agent on every node.

## Prerequisites

- `skfence` or `skfenceha` deployed (provides `cloud-socket-proxy-<env>` and
  the `socket-proxy` service).
- `cloud-public-<env>` network present (created by the Traefik stack).

## Security

- **No Docker socket bind mount.** Docker access is via the socket-proxy
  pattern only (see Overview).
- **No baked admin password.** `skport.env.j2` deliberately sets no
  `ADMIN_PASSWORD`; a literal default would mean every instance that forgets
  to override it shares the same credential. Instead this fails closed onto
  Portainer's own first-run setup: the first browser to reach the UI within
  5 minutes of the container starting is prompted to create the admin
  account interactively. If nobody visits in time, Portainer requires a
  container restart to retry initial setup rather than silently exposing an
  open instance.
- TLS is terminated at Traefik (`tls.certresolver=main`); Portainer itself
  serves plain HTTP on 9000 internally.

## Deployment

```bash
ansible-playbook -e env=prod \
  -i v1/ansible/shared/hosts \
  v1/ansible/optional/skport/deploy_skport-prod.yml \
  --vault-password-file "$HOME/.vault_pass_env/.prod_vault_pass"
```

Swap `prod` for `staging` or `dev` (and the matching playbook/vault
password) for the other environments.

## Configuration (vault)

```yaml
skport:
  CLUSTERNAME: "skstack01"
  DOMAIN: "example.com"
  portainer_version: "lts"   # Portainer CE LTS tag; never :latest
  TZ: "America/New_York"
```

`portainer_version` defaults to `lts`, the Portainer CE long-term-support
tag, so a fresh deploy never pulls a moving `:latest` image.

## Networks

| Network | Purpose |
|---|---|
| `cloud-public-<env>` | Traefik ingress (framework-shared subnet) |
| `cloud-socket-proxy-<env>` | Access to skfence/skfenceha's socket-proxy (framework-shared subnet) |
| `skport-<env>` | Service-local overlay, allocated from the reserved `172.16.250.0/24`-`172.16.254.0/24` band |

## Verification

```bash
docker service ls | grep skport
docker service logs skport-<env>_portainer --tail 50
curl -I https://skport.<cluster>.<domain>
```
