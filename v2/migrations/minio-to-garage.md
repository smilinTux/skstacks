# Migration 2 — MinIO → Garage

**Risk: Medium.** Not drop-in — different storage backends. Both speak S3, so the
migration is an **S3-to-S3 copy** while both run, then repoint apps. MinIO
community was **archived Apr 2026** (→ proprietary AIStor), so this is not optional.

## Precheck
- Inventory buckets + sizes: `mc ls --recursive minio/` (or `mc du`).
- Note every app that holds MinIO S3 credentials/endpoints (repoint list).
- Decide HA: Garage 3-node `replication_factor=3` (see skobject stack) vs single
  node `=1`. If you need **Object-Lock/WORM** immutability, use SeaweedFS instead
  (Garage lacks Object-Lock as of 2026).

## Backup / source retention
In an S3-to-S3 copy the **source MinIO IS the backup** — it is never mutated by
the migration. Keep it intact and read-only through the rollback window. For extra
safety also snapshot the MinIO data dir before starting:
```bash
mc admin cluster bucket export minio/ > /backup/minio-buckets-$(date +%F).json  # metadata
tar czf /backup/minio-data-$(date +%F).tgz /data/minio                          # raw (offline)
```

## Stand up Garage (alongside MinIO)
```bash
docker stack deploy -c platform/swarm/stacks/skobject/docker-compose.yml skobject
garage status                                  # nodes healthy
garage bucket create <bucket>; garage key create app-key
garage bucket allow --read --write <bucket> --key app-key
```

## Copy the data (both running)
```bash
# rclone with two S3 remotes (minio: and garage:) — verifies checksums
rclone sync minio:<bucket> garage:<bucket> --checksum --transfers 8 --progress
# repeat per bucket; for huge sets, run incremental syncs until the delta is ~0
```
(`mc mirror minio/<bucket> garage/<bucket>` also works if you prefer the MinIO client.)

## Cutover
1. Final incremental `rclone sync` with writers paused (small delta).
2. Repoint every app's S3 endpoint + keys to Garage (resolve creds from the
   secret backend — `garage_s3_access_key`/`garage_s3_secret_key`).
3. Verify object counts + a checksum spot-check per bucket:
   ```bash
   rclone check minio:<bucket> garage:<bucket> --checksum
   ```

## Verify
- `rclone check` reports 0 differences per bucket.
- App read/write smoke tests against Garage pass.

## Rollback
Apps still have the MinIO endpoint config; repoint back. Keep MinIO read-only for
the rollback window, then decommission once Garage is proven.
