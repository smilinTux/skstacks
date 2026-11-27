# skstor (Garage S3-compatible object storage)

Single-node [Garage](https://garagehq.deuxfleurs.fr/) on Docker Swarm,
replacing MinIO. See `docs/decisions/skstor-backend.md` at the repo root for
why: MinIO's Community Edition repo is archived and its Docker Hub images
were deleted 2026-09-11.

## Vault contract

Required (no default -- the deploy fails closed without these):

| Key | Notes |
|---|---|
| `skstor.CLUSTERNAME` | Used to build the public hostname `skstor[-env].<cluster>.<domain>`. |
| `skstor.DOMAIN` | Same. |
| `skstor.rpc_secret` | Random 32-byte hex string, unique per instance: `openssl rand -hex 32`. Node-to-node RPC auth, **not** an S3 credential -- Garage has no MinIO-style root user/password at all. |

Optional (sane defaults, see `src/config/skstor/*.j2`):
`skstor.GARAGE_IMAGE_REF`, `skstor.REPLICATION_FACTOR` (default `1`, single
node), `skstor.GARAGE_S3_REGION` (default `garage`), `skstor.CPU_LIMIT`,
`skstor.MEMORY_LIMIT`, `skstor.CPU_RESERVATION`, `skstor.MEMORY_RESERVATION`,
`skstor.TZ`, `skstor.RUST_LOG`, `skstor.INSTANCE`, `skstor.APP_ENV`,
`skstor.CLOUDFLARED`, `skstor.networks` (overrides the registered
`skstor-<env>` / `cloud-public-<env>` overlay networks),
`skstor.db_engine` (default `sqlite`; see "Storage layout" below),
`skstor.meta_dir` / `skstor.data_dir` (default the bind-mount paths Garage
sees inside the container, `/var/lib/garage/meta` and `/var/lib/garage/data`),
`skstor.META_NODE`, `skstor.META_PATH`, `skstor.META_SNAPSHOT_INTERVAL` (node-local
metadata, see "Storage layout" below),
`skstor.TRAEFIK_MIDDLEWARES` (the web router's whole middleware chain, default
`default-no-error-pages@file`, so S3 clients get Garage's own XML errors; see "Router middleware overrides" in
`docs/v1-service-catalog.md`).

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skstor/deploy_skstor-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

Swap `prod` for `staging` or `dev` (and the matching playbook/vault
password/inventory) for the other environments.

## Storage layout

By default metadata and data bind-mount onto `/var/data/skstor-<env>/{meta,data}`
on this instance's shared filesystem (NFS, matching every other v1 service and
this framework's own `/var/data` requirement), not a named Docker volume,
which would be host-local and strand data on whichever manager this
replicas=1 service last ran on after a Swarm reschedule.

**Production: keep the metadata db off NFS.** Garage's metadata db (sqlite or
lmdb) needs real file locking and fast fsync. On NFS a rescheduled task can
find the old task's lock (`Error: Unable to open metadata db: database error:
Sqlite: timed out waiting for connection`, exit 1) and slow fsyncs make the
`garage status` healthcheck time out (exit 137, unhealthy), so the service
restarts in a loop. Put the db on one node's local disk:

| Key | Default | Notes |
|---|---|---|
| `skstor.META_NODE` | empty | Swarm node (inventory name = swarm hostname) that holds the metadata. Pins the service there (`node.hostname == <node>`) and the deploy creates `META_PATH` on it. Required when `META_PATH` is outside `/var/data`; the deploy fails closed otherwise. |
| `skstor.META_PATH` | `/var/data/skstor-<env>/meta` | Host side of the metadata bind mount, for example `/opt/skstor-<env>/meta` on local disk. |
| `skstor.META_SNAPSHOT_INTERVAL` | `6h` with `META_NODE`, else empty | Garage `metadata_auto_snapshot_interval`. Snapshots go to `/var/data/skstor-<env>/meta-snapshots` (shared tree), so losing the node does not lose buckets and keys. Set `""` to turn off. |

Data blocks stay on `/var/data/skstor-<env>/data` (shared); they are plain
files written once, which NFS handles. With `META_NODE` set the service
no longer fails over to another node: if that node is down, Garage is down
until it is back or you recover the metadata elsewhere (framework
`docs/runbooks/skstor.md`).

`skstor.db_engine` defaults to `sqlite`, not Garage's own default
`lmdb`. Per Garage's docs
(<https://garagehq.deuxfleurs.fr/documentation/reference-manual/configuration/#db_engine>),
LMDB "is prone to database corruption after an unclean shutdown (e.g. a
process kill or a power outage)", while "Sqlite ... does not have the
issues listed above for LMDB" (slower, but this is a single-writer instance,
not a high-throughput cluster). Keep sqlite unless you have measured a need
for LMDB; with `META_NODE` and snapshots in place it is an operator choice.

Moving an existing instance's metadata to local disk needs the service
stopped while the db is copied: follow `docs/runbooks/skstor.md` (framework
root), never just flip the vars on a running instance (Garage would start on
an empty dir and look like a new cluster).

## One-time bootstrap (per instance, after the first deploy)

The deploy playbook already assigns and applies a single-node cluster layout
automatically, once the stack is up -- Garage refuses all S3 traffic until a
layout exists, so this step is not optional and not manual. The bootstrap
runs as an Ansible task (`bootstrap.sh.j2`) delegated to whichever swarm node
Swarm actually scheduled the garage task on, since that node is not always
the manager the playbook itself runs on.

What is still a manual, per-consumer, one-time step (Garage has no admin
root credential to hand out, unlike MinIO):

```bash
# Run against the running garage container, e.g.:
#   docker exec -ti $(docker ps -q -f name=skstor-<env>_garage) /garage ...

garage bucket create <bucket-name>
garage key create <consumer-name>-key
garage bucket allow --read --write <bucket-name> --key <consumer-name>-key
garage key info <consumer-name>-key   # prints the access/secret key pair once
```

Put the resulting access key ID / secret access key into that consumer's own
vault (e.g. `skhub.s3_access_key` / `skhub.s3_secret_key`), pointed at
`https://skstor[-env].<cluster>.<domain>` (port 443 via Traefik, path-style
addressing). See `docs/decisions/skstor-backend.md` for the full consumer
migration list (skhub, skform, skblock/skfile).

skhub specifically has a one-line shortcut (`skhub.storage_backend: skstor`)
that skips the public hostname and reaches this service directly over the
`skstor-<env>` overlay network instead -- see
`../skhub/README.md` for the exact vault keys.

## What changed vs. the old MinIO skstor

- No web console (MinIO shipped one on port 9001; Garage does not ship an
  admin UI). Cluster/bucket/key administration is the `garage` CLI via
  `docker exec`, or `mc`/`aws s3`/`rclone` from outside for data operations.
- No S3 bucket versioning support in Garage. If a consumer needs
  versioning or Object-Lock/WORM immutability, use SeaweedFS instead (see
  the decision doc) rather than relying on Garage for that guarantee.
- Root credentials are gone; each consumer gets its own scoped key (see
  bootstrap above) instead of reusing one MinIO root user/password.

`skstor.layout_capacity` (default `1G`): capacity passed to `garage layout assign` for the single node. Garage v2 rejects values under 1K; with one node it is only a relative weight.
