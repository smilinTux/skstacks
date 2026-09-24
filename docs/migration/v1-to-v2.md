# Migration: v1 → v2

## Why
- Monorepo
- Multi-platform (Swarm + K8s)
- GitOps-ready
- Shared modules

## Key Changes
| v1 | v2 |
|----|----|
| `ansible/core/` | `v2/infra/ansible/` |
| `apps/` | `v2/apps/` |
| Direct `docker-compose.yml` | `docker-compose.yml` + `overlays/` |

## Example: skfence
```bash
# v1
ansible/core/skfence/

# v2
v2/apps/skfence/
v2/overlays/swarm/prod/skfence.yml
