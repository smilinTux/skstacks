# skreg: private Docker registry (Registry v2)

A cluster-local image registry for images we build ourselves (app workers,
patched images) that should not go to a public registry.

## How it is reached

The registry is published on port 5000 (`skreg.PUBLISHED_PORT`) through the
swarm **ingress mesh**, so it answers on `127.0.0.1:5000` on **every** swarm
node. dockerd treats loopback registries as insecure-allowed by default, so
nodes pull without TLS, without a `daemon.json` change and without restarting
docker:

```yaml
services:
  worker:
    image: 127.0.0.1:5000/fwe-worker:20261016-1
```

Data lives on NFS-backed `/var/data/skreg-<env>/data`, so every replica serves
the same blobs. All replicas must share `REGISTRY_HTTP_SECRET` (the playbook
refuses to deploy without one).

## Pushing from outside the swarm

Port 5000 is not exposed publicly and has no auth layer: treat it as reachable
by anything that can reach a node's port 5000 (LAN / tailnet), and keep it off
any router port-forward or tunnel. To push from a build box that is not a
swarm node, tunnel the port over SSH so the push also targets loopback:

```bash
ssh -f -N -o ExitOnForwardFailure=yes -L 5000:127.0.0.1:5000 <user>@<any-swarm-node>
docker tag fwe-worker:20261016-1 127.0.0.1:5000/fwe-worker:20261016-1
docker push 127.0.0.1:5000/fwe-worker:20261016-1
```

Always push a pinned tag (no `latest`) and reference that tag in the
consuming stack.

## Vault keys

| key | required | default | notes |
|-----|----------|---------|-------|
| `skreg.REGISTRY_HTTP_SECRET` | yes | none | long random string (`openssl rand -hex 32`); legacy top-level `vault_skreg_registry_http_secret` is still accepted |
| `skreg.PUBLISHED_PORT` | no | `5000` | ingress-published port |
| `skreg.MODE` | no | `global` | `global` or `replicated` (then `skreg.REPLICAS`, default 1) |
| `skreg.REGISTRY_VERSION` | no | `2.8.3` | image tag |
| `skreg.STORAGE_DELETE_ENABLED` | no | `true` | allow manifest deletes (needed for garbage collection) |
| `skreg.placement_constraints` | no | `[]` | extra swarm constraints |
| `skreg.networks` | no | `skreg-<env>` on `172.16.62.0/24` (prod), `.146` (staging), `.147` (dev) | |

## Garbage collection

Deleting tags does not free disk until a GC run:

```bash
docker exec $(docker ps -q -f name=skreg-prod_registry | head -1) \
  registry garbage-collect /etc/docker/registry/config.yml --delete-untagged
```

Run it while nothing is pushing.
