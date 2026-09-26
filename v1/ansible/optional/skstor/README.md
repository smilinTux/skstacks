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
`skstor-<env>` / `cloud-public-<env>` overlay networks).

## One-time bootstrap (per instance, after the first deploy)

The deploy script (`src/skstor/deploy.j2`) already assigns and applies a
single-node cluster layout automatically -- Garage refuses all S3 traffic
until a layout exists, so this step is not optional and not manual.

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

## What changed vs. the old MinIO skstor

- No web console (MinIO shipped one on port 9001; Garage does not ship an
  admin UI). Cluster/bucket/key administration is the `garage` CLI via
  `docker exec`, or `mc`/`aws s3`/`rclone` from outside for data operations.
- No S3 bucket versioning support in Garage. If a consumer needs
  versioning or Object-Lock/WORM immutability, use SeaweedFS instead (see
  the decision doc) rather than relying on Garage for that guarantee.
- Root credentials are gone; each consumer gets its own scoped key (see
  bootstrap above) instead of reusing one MinIO root user/password.
