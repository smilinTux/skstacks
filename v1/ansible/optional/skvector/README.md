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

## Router middlewares

Both Traefik routers read their whole middleware chain from the instance
vault; unset (or empty) renders exactly as before:

| Key | Router | Default |
|---|---|---|
| `skvector.TRAEFIK_MIDDLEWARES` | HTTP (`skvector[-env]`, port 6333) | `skvector[-env]-buffering,default-no-error-pages@file` |
| `skvector.TRAEFIK_MIDDLEWARES_GRPC` | gRPC (`skvector[-env]-grpc`, port 6334) | `default-no-error-pages@file` |

The value replaces the chain, it is not appended: keep
`skvector[-env]-buffering` in the HTTP chain when you override it (for example
`skvector-buffering,crowdsec-bouncer@file,default@file` on prod).

## Web UI behind SSO

The Qdrant web UI (`/dashboard`) keeps an API key in the browser and sends it
as an `api-key` header on every REST call to its own origin (`/collections`
and so on). To open it from the Authentik portal already signed in, turn on a
second host that Traefik fronts with SSO and that injects the key itself:

```yaml
skvector:
  QDRANT_READ_ONLY_API_KEY: "<random, from the vault>"   # Qdrant read-only key
  UI_HOST_ENABLED: true
  # UI_KEY_MODE: read_only    # default; full injects QDRANT_API_KEY instead
```

| Key | Default | Meaning |
|---|---|---|
| `QDRANT_READ_ONLY_API_KEY` | empty | Renders `QDRANT__SERVICE__READ_ONLY_API_KEY` (Qdrant `service.read_only_api_key`). A request with this key can read but not write. Usable on its own by read-only clients. |
| `UI_HOST_ENABLED` | `false` | Adds router `skvector-ui[-env]` on `Host(skvector-ui[-env].<cluster>.<domain>)` to the same 6333 service. The main `skvector[-env]` router and the gRPC router are not touched. Off renders the compose file exactly as before. |
| `UI_KEY_MODE` | `read_only` | `read_only` injects `QDRANT_READ_ONLY_API_KEY`; `full` injects `QDRANT_API_KEY`. The deploy stops with a clear message when the key for the mode is empty. |
| `UI_SSO_MIDDLEWARE` | `authentik@file` | Forward-auth middleware on the UI router. It can name another forward-auth middleware but cannot be turned off (`none`/`false` stops the deploy). |

The UI router chain is `<UI_SSO_MIDDLEWARE>,skvector[-env]-ui-apikey,skvector[-env]-buffering,default-no-error-pages@file`:

- `authentik@file` (core/skfenceha) is attached EXPLICITLY. The UI host never
  relies on the domain-wide forward-auth catch-all, so it stays behind SSO even
  where the catch-all is off. Forward auth runs first, so an unauthenticated
  request is redirected to sign in before the key is ever added.
- `skvector[-env]-ui-apikey` sets `headers.customRequestHeaders.api-key`.
  Traefik SETS the header (Go `Header.Set`), which replaces any `api-key` the
  client sent, so a user cannot pick another key through this host.
- `default-no-error-pages@file`, as on the main router: the dashboard reads
  Qdrant's JSON errors (a 403 on a write with the read-only key, a 404 for a
  missing collection), which the error-pages middleware would replace with HTML.

When the UI host is on, the portal tile opens
`https://skvector-ui[-env].<cluster>.<domain>/dashboard#/collections`;
otherwise it opens the main host as before. The tile group stays
`AUTHENTIK_TILE_GROUP` (default `skadmin`).

Security notes:

- The injected key is a Traefik label on the `qdrant` service. Anyone who can
  read the service spec (swarm managers, `docker service inspect`, Portainer)
  or the Traefik API and dashboard can read it. That is why `read_only` is the
  default: a leaked read-only key can read vectors and payloads but not change
  or delete them. Use `full` only when the admins who get the UI should also
  write, and treat that label like the main key.
- Anyone who passes SSO on the UI host acts with the injected key. Grant the
  UI host (the tile group and the forward-auth application binding) to admin
  groups only, never to "any signed-in user".
- Programmatic clients keep using `skvector[-env].<cluster>.<domain>` with
  their own key; do not point them at the UI host.

Rotating the injected key: set a new `QDRANT_READ_ONLY_API_KEY` (or
`QDRANT_API_KEY` in `full` mode) in the vault and redeploy skvector. The env
file and the label change in the same deploy; Qdrant restarts with the new key
and the old one stops working. Update any read-only client that used the old
key first. To turn the UI host off, set `UI_HOST_ENABLED: false` and redeploy:
the router, the header middleware and the label carrying the key go away.

## Resource limits

The service limits and reservations come from the instance vault; unset keys
render the defaults below, so existing instances are unchanged:

| Key | Default |
|---|---|
| `skvector.RESOURCES_LIMITS_CPUS` | `2.00` |
| `skvector.RESOURCES_LIMITS_MEMORY` | `4096M` |
| `skvector.RESOURCES_RESERVATIONS_CPUS` | `0.50` |
| `skvector.RESOURCES_RESERVATIONS_MEMORY` | `1024M` |

Set them in the vault, not with `docker service update`: a stack deploy
re-applies the compose file and reverts any live change. Qdrant memory-maps
its segments, so a multi-million-point collection that does not fit in the
memory limit thrashes (high CPU, filtered counts and searches timing out).
Size the memory limit to the collection, for example 12288M for a 7.7M-point
collection.

## Payload indexes

Collections and their payload indexes live in Qdrant storage, not in the
stack, so a deploy neither creates nor removes them. Create the indexes your
filters need once per collection, and again after a restore or recreate:

    curl -X PUT "$QDRANT_URL/collections/<collection>/index?wait=true" \
      -H "api-key: $QDRANT_API_KEY" -H "Content-Type: application/json" \
      -d "{\"field_name\": \"file_path\", \"field_schema\": \"keyword\"}"

Verify with `GET /collections/<collection>` (`payload_schema`). Without an
index, filtered counts and searches scan every point.
