# Migration 1 — Redis → Valkey

**Risk: Low.** Valkey 8 is a fork of Redis 7.2 (before the relicense) and is
drop-in: same RESP protocol, same `RDB`/`AOF` on-disk formats, same config
directives, same client libraries. No data transformation needed.

## Precheck
- Confirm v1 uses `redis:7` / `redis:7.2.4` (not a Redis ≥7.4 feature that
  post-dates the fork — Valkey 8 covers everything through 7.2).
- Note clients of the cache (skchat-fallback, sessions, queues) for the cutover.
- `redis-cli INFO persistence` → confirm AOF/RDB enabled.

## Backup
```bash
redis-cli -a "$REDIS_PASSWORD" BGSAVE          # snapshot to dump.rdb
cp /var/lib/redis/dump.rdb  /backup/redis-$(date +%F).rdb
```

## Cutover
1. Stop writers (or accept a brief read-only window).
2. Bring up Valkey pointed at the SAME data dir / volume:
   ```bash
   docker stack deploy -c platform/swarm/stacks/skcache/docker-compose.yml skcache
   # valkey-server loads the existing dump.rdb / appendonly.aof unchanged
   ```
3. Verify: `valkey-cli -a "$VALKEY_PASSWORD" DBSIZE` matches the pre-cutover
   `redis-cli DBSIZE`; spot-check a few keys.
4. Repoint clients to the Valkey service name/endpoint (k8s: skcache Service;
   swarm: `skcache` on the overlay net). Connection strings are identical.

## Verify
- `DBSIZE` parity, `INFO replication`, app health checks green.
- Run for one rollback window (e.g. 48h) with the old Redis volume retained.

## Rollback
Re-point clients to the old Redis; the RDB/AOF were never mutated by Valkey in a
backward-incompatible way (same formats), so the original volume is intact.

## Live-migration alternative (zero downtime)
Run Valkey as a **replica of** the live Redis (`REPLICAOF <redis> <port>`), let it
sync, then promote Valkey (`REPLICAOF NO ONE`) and cut clients over — no write
freeze required.
