# skvector (Qdrant)

`skvector` runs one Qdrant (`qdrant/qdrant`) replica, a vector database, behind
Traefik at `skvector.<cluster>.<domain>` (HTTP on 6333, gRPC on 6334).

Deploy: `ansible-playbook deploy_skvector-<env>.yml` (dev, staging, prod).

By default Qdrant's storage (`/qdrant/storage`) and snapshots
(`/qdrant/snapshots`) are bind mounts of `/var/data/skvector-<env>/storage` and
`/var/data/skvector-<env>/snapshots` (shared storage, NFS on a typical
instance), and the service floats on any node matching
`skvector.placement_constraints`.

## Local data on a pinned node

Qdrant memory-maps its segments and writes a WAL for every update, so on NFS
the mount's latency is the database's latency. To keep the data on local disk
on one node:

```yaml
skvector:
  DATA_NODE: <node hostname>                       # also an inventory host
  STORAGE_PATH: /var/lib/skvector-<env>/storage
  SNAPSHOTS_PATH: /var/lib/skvector-<env>/snapshots
```

| Key | Default | Meaning |
|---|---|---|
| `DATA_NODE` | empty | Node hostname: adds `node.hostname == <DATA_NODE>` to `qdrant`, after `placement_constraints` (not added twice if `placement_constraints` already has it). Required with a local `STORAGE_PATH`/`SNAPSHOTS_PATH`: the play stops without it, because local data on a floating service is an empty database after the next reschedule. Set alone (paths on `/var/data`) it only pins the service. |
| `STORAGE_PATH` | `/var/data/skvector-<env>/storage` | Host dir bound at `/qdrant/storage`. A path outside `/var/data` is local disk on `DATA_NODE`. |
| `SNAPSHOTS_PATH` | `/var/data/skvector-<env>/snapshots` | Host dir bound at `/qdrant/snapshots`. Same rule. Keeping snapshots on shared storage while storage is local is allowed (and keeps the snapshots off the node). |

With unset keys the compose file renders exactly as before. The compose
file's `skvector-<env>-storage`/`-snapshots` named volumes (bind `device`)
follow the same two paths.

A path outside `/var/data` is created on `DATA_NODE` (not recursive,
1000:1000 0755, the same as the shared-storage copy; `qdrant/qdrant` runs as
0:0, so the owner does not gate access, it keeps the two copies identical and
fits the unprivileged image) and the old `/var/data/skvector-<env>/...` path
is no longer created. Local data is not on shared storage: back it up with
the node, or take Qdrant snapshots into a `SNAPSHOTS_PATH` on shared storage.

### Migrating a running instance

1. Pick `DATA_NODE` (normally the node already running `qdrant`) and check its
   local free space against `du -sh /var/data/skvector-<env>/{storage,snapshots}`.
2. Note the collections and their point counts (`GET /collections`, then
   `GET /collections/<name>` for `points_count`). Stop writers, then scale
   `qdrant` to 0 (`docker service scale skvector-<env>_qdrant=0`) and wait
   until no task is running.
3. On `DATA_NODE`: `rsync -aHAX --numeric-ids /var/data/skvector-<env>/storage/
   <storage path>/` (same for snapshots if it moves too). Verify: file counts
   and total sizes match on both sides (`find <dir> | wc -l`, `du -s
   --apparent-size <dir>`).
4. Set `DATA_NODE`, `STORAGE_PATH` and `SNAPSHOTS_PATH` in the instance vault
   and deploy skvector from a framework pin that has these keys (redeploy the
   stack from the template; do not swap the mounts by hand). Check the service
   is 1/1 on `DATA_NODE`, `/healthz` answers and every collection reports the
   point count from step 2.
5. Rename each moved shared-storage dir, for example
   `mv /var/data/skvector-<env>/storage
   /var/data/skvector-<env>/storage.STALE-moved-to-<node>-local-<date>`, so a
   deploy from an older pin that still binds the old path fails loudly (no
   directory to bind-mount) instead of starting on stale data. Leave it in
   place as the rollback copy. Never delete it.

Rollback: scale to 0, rename the `.STALE-...` dirs back (after copying any
newer local data into them), clear the three keys and redeploy.
