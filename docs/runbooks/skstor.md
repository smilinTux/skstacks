# Runbook: skstor (Garage) metadata

Role: [`v1/ansible/optional/skstor/`](../../v1/ansible/optional/skstor/README.md).
Background: [`docs/decisions/skstor-backend.md`](../decisions/skstor-backend.md).

## The rule

**Garage's metadata db never lives on NFS in production.** Set
`skstor.META_NODE` and `skstor.META_PATH` (local disk on that node). Data blocks
may stay on the shared `/var/data/skstor-<env>/data`.

Why: the metadata db (sqlite by default, or lmdb) needs real file locks and
fast fsync. On NFS:

- a rescheduled task finds the previous task's lock and exits 1 with
  `Error: Unable to open metadata db: database error: Sqlite: timed out waiting for connection`;
- slow fsyncs make the `garage status` healthcheck time out, and Swarm kills
  the task (`non-zero exit (137): dockerexec: unhealthy container`).

Each restart is another unclean shutdown, so it loops.

With `META_NODE` set:

- the service is pinned to `node.hostname == <META_NODE>`;
- the deploy creates `META_PATH` on that node;
- Garage writes a metadata snapshot every `META_SNAPSHOT_INTERVAL` (default
  `6h`) to the shared `/var/data/skstor-<env>/meta-snapshots`.

The deploy refuses to run when `META_PATH` is outside `/var/data` without
`META_NODE`, and when `META_PATH` has no `node_key` while the old shared
`/var/data/skstor-<env>/meta` still does (the db was not copied).

Trade-off: no automatic failover. If `META_NODE` is down, skstor is down until
the node is back or you recover on another node (below).

## Move the metadata to local disk (existing instance)

Needs a framework tag that has `META_NODE` (see CHANGELOG). Do it under the
prod deploy lock. Placeholders: `<env>`, `<node>` (the target swarm node),
`<local>` (for example `/opt/skstor-<env>/meta`), `<manager>`.

1. **Record the state.** On the node running Garage:
   `C=$(docker ps -q -f label=com.docker.swarm.service.name=skstor-<env>_garage)`, then
   `docker exec $C /garage status`, `/garage layout show`, `/garage bucket list`,
   `/garage key list`. Save the output. Check which consumers use the
   buckets and warn them of the short outage.
2. **Stop Garage cleanly** (on a manager):
   `docker service scale skstor-<env>_garage=0` and wait until
   `docker service ps skstor-<env>_garage` shows no running task. The db must
   be closed before it is copied; never copy a live sqlite db with `cp`.
3. **Back up the shared copy** (on the shared tree, any node):
   `sudo cp -a /var/data/skstor-<env>/meta /var/data/skstor-<env>/meta.bak-$(date +%Y%m%d-%H%M)`.
   Check it: `sudo diff -r /var/data/skstor-<env>/meta <backup>` prints nothing,
   and `node_key`, `cluster_layout`, `data_layout`, `db.sqlite` are all present.
   (Cluster nodes usually have no `sqlite3`; a byte-identical copy of a db that
   Garage closed cleanly is the check.)
4. **Copy to the node's local disk** (on `<node>`):
   `sudo mkdir -p <local> && sudo cp -a /var/data/skstor-<env>/meta/. <local>/`,
   then `sudo sh -c 'cd /var/data/skstor-<env>/meta && sha256sum *'` and the same in
   `<local>`: every line must match.
   Leave the shared `meta/` in place: it is the fallback until the move is verified.
5. **Set the instance vault**: `skstor.META_NODE: <node>`,
   `skstor.META_PATH: <local>` (optional `META_SNAPSHOT_INTERVAL`). Merge it
   with the framework pin bump through the instance PR.
6. **Deploy** the skstor playbook from a fresh worktree of the merged instance
   `main`, `--check --diff` first. The stack deploy brings the service back at
   1 replica on `<node>`.
7. **Verify**:
   - `docker service ps skstor-<env>_garage` runs on `<node>`;
   - `docker exec $C /garage status` shows the **same node id** as step 1
     (a new id means Garage started on an empty dir: stop at once, see below);
   - `/garage bucket list` and `/garage key list` match step 1;
   - an S3 read of a known object succeeds;
   - `docker exec $C /garage meta snapshot` writes `<timestamp>/db.sqlite` under
     `/var/data/skstor-<env>/meta-snapshots/`;
   - no new tasks in `docker service ps` for at least 15 minutes.
8. After a few days of clean running, delete `meta.bak-*` and the old shared
   `meta/` (the deploy guard reads the old `meta/node_key`, so keep `meta/`
   until you are sure, or remove both together).

If step 7 shows a new node id or missing buckets: scale to 0, revert the vault
change (or `docker service update --constraint-rm` plus the old mount by
redeploying the previous instance commit), and Garage starts again on the
untouched shared `meta/`.

## Recover when `META_NODE` is lost

The bucket and key records live in the metadata db; the objects live in
`/var/data/skstor-<env>/data`. To bring skstor up on another node:

1. `docker service scale skstor-<env>_garage=0`.
2. On the new node, create `<local>` and restore the metadata into it:
   - copy `node_key`, `node_key.pub`, `cluster_layout`, `data_layout` from the
     old `/var/data/skstor-<env>/meta/` (or a `meta.bak-*`): they change only on a
     layout change, so the migration copy is still valid unless you changed the
     layout since;
   - each snapshot is a directory `/var/data/skstor-<env>/meta-snapshots/<UTC timestamp>/`
     holding only the db (`db.sqlite` for sqlite). Copy the newest one's
     `db.sqlite` to `<local>/db.sqlite`. For lmdb, see Garage's
     "Recovering from failures" doc on metadata snapshots.
3. Set `skstor.META_NODE` to the new node, deploy, and run the step 7 checks.
   Objects written after the snapshot have blocks in `data/` but no entry in
   the db, so they are lost; run `garage repair --yes blocks` afterwards so
   Garage resyncs its block set against the restored db.

With no snapshot at all, the objects cannot be listed again from `data/`
alone. That is why `META_SNAPSHOT_INTERVAL` defaults on.
