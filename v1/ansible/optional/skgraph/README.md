# skgraph (FalkorDB)

`skgraph` runs one FalkorDB (`falkordb/falkordb`) replica, a graph database
spoken to over the Redis protocol. It has no web UI: clients reach it on the
`skgraph-<env>` overlay network or on the published ingress port
(`skgraph.PUBLISHED_PORT`, default `16379`).

Deploy: `ansible-playbook deploy_skgraph-<env>.yml` (dev, staging, prod).

By default the FalkorDB data dir (`/var/lib/falkordb/data`, RDB and AOF) is a
bind mount of `/var/data/skgraph-<env>/data` (shared storage, NFS on a typical
instance), and the service floats on any worker.

## Local data on a pinned node

Every FalkorDB write and AOF fsync goes through the bind mount, so on NFS its
latency is the database's write latency. To keep the data on local disk on
one node:

```yaml
skgraph:
  DATA_NODE: <node hostname>               # also an inventory host
  DATA_PATH: /var/lib/skgraph-<env>/data
```

| Key | Default | Meaning |
|---|---|---|
| `DATA_NODE` | empty | Node hostname: adds `node.hostname == <DATA_NODE>` to `falkordb`, after its `node.role == worker` constraint. Required with a local `DATA_PATH`: the play stops without it, because local data on a floating service is an empty database after the next reschedule. Set alone (with `DATA_PATH` on `/var/data`) it only pins the service. |
| `DATA_PATH` | `/var/data/skgraph-<env>/data` | Host dir bound at `/var/lib/falkordb/data`. A path outside `/var/data` is local disk on `DATA_NODE`. |

With unset keys the compose file renders exactly as before.

A path outside `/var/data` is created on `DATA_NODE` (not recursive, root:root
0755, the same as the shared-storage copy: `falkordb/falkordb` runs as root)
and the old `/var/data/skgraph-<env>/data` is no longer created. Local data is
not on shared storage: back it up with the node.

### Migrating a running instance

1. Pick `DATA_NODE` (normally the node already running `falkordb`) and check
   its local free space against `du -sh /var/data/skgraph-<env>/data`.
2. Stop writers, then scale `falkordb` to 0 (`docker service scale
   skgraph-<env>_falkordb=0`) and wait until no task is running, so the RDB
   and AOF on disk are final.
3. On `DATA_NODE`: `rsync -aHAX --numeric-ids /var/data/skgraph-<env>/data/
   <path>/` (`<path>` is the new `DATA_PATH`). Verify: file counts and total
   sizes match on both sides (`find <dir> | wc -l`, `du -s --apparent-size
   <dir>`).
4. Set `DATA_NODE` and `DATA_PATH` in the instance vault and deploy skgraph
   from a framework pin that has these keys (redeploy the stack from the
   template; do not swap the mount by hand). Check the service is 1/1 on
   `DATA_NODE` and the graph answers (`redis-cli GRAPH.LIST`, a known query).
5. Rename the old shared-storage dir, for example
   `mv /var/data/skgraph-<env>/data
   /var/data/skgraph-<env>/data.STALE-moved-to-<node>-local-<date>`, so a
   deploy from an older pin that still binds the old path fails loudly (no
   directory to bind-mount) instead of starting on stale data. Leave it in
   place as the rollback copy. Never delete it.

Rollback: scale to 0, rename the `.STALE-...` dir back (after copying any
newer local data into it), clear the two keys and redeploy.
