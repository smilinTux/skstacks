# v1 → v2 migrations

SKStacks v2 is mostly *additive* over v1 (secrets/PKI/IDS/bus/chat/voice/mesh/
observability are net-new). But three v1 components are **superseded or dead** and
need a real data migration. Run them in this order (lowest → highest risk):

| # | Migration | v1 → v2 | Risk | Why |
|---|---|---|---|---|
| 1 | [Redis → Valkey](redis-to-valkey.md) | `redis:7` → `valkey/valkey:8` | **Low** (drop-in) | Redis relicensed; Valkey is the BSD-3 fork, RDB/AOF + protocol compatible |
| 2 | [MinIO → Garage](minio-to-garage.md) | `quay.io/minio/minio` → `dxflrs/garage` | **Medium** (data copy) | MinIO community **archived Apr 2026** → proprietary AIStor |
| 3 | [PG16/pgvecto-rs → PG17-unified](pg-to-pg17-unified.md) | `pgvector/pgvector:pg16` + `pgvecto-rs` → `skmem-pg:pg17-bm25-age` | **High** (DB upgrade) | One PG17 engine: pgvector + ParadeDB pg_search + Apache AGE; pgvecto-rs superseded |

**Golden rules**
- v1 stays **frozen-prod** until each capability is verified-cut-over (no big-bang).
- Every migration: **precheck → backup → cutover → verify → keep-rollback-window**.
- Secrets come from the secret backend (vault-file / OpenBao), never inline.
- v1-only apps (geth/lighthouse/thirdweb/Plex) are *not* v2 ports — leave them in
  the prod app layer; don't migrate them into v2 core.
