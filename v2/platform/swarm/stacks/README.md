# Swarm core-service stacks

The simple-tier deploy target. The same `compute/`/`core/`/`cloud/` capability
descriptors that render to K8s manifests also render to these Docker Swarm
stacks — one service definition, two render targets.

| Stack | Port | Image (ratified) | Notes |
|---|---|---|---|
| `traefik/` | skfence | `traefik:v3` | Edge + Gateway; ACME-master + global workers (HA) |
| `skcache/` | skcache | `valkey/valkey` | **Valkey**, not Redis (BSD-3 fork) |
| `skobject/` | skobject | `dxflrs/garage` | **Garage**, not MinIO (archived 2026); 3×RF=3 = HA |
| `sksec/` | sksec | `crowdsecurity/crowdsec` | Tier-1 defense, global mode (one/node) |

## Deploy
```bash
# 1. create the shared overlay network once (referenced as external by every stack)
docker network create --driver overlay --attachable skstacks
# 2. deploy a stack (env from .env / Ansible group_vars)
docker stack deploy -c stacks/skcache/docker-compose.yml skcache
```

All stacks attach to the external `skstacks` overlay network and pull secrets
(passwords, bouncer keys, Garage rpc_secret) from the secret backend at deploy
time (vault-file / OpenBao via Ansible). HA tiering (block/file storage) follows
the skstorage family — on Swarm (no CSI), use host-local volumes + app-level
replication or JuiceFS-FUSE mounts (see compute/SKSTORAGE.md).
