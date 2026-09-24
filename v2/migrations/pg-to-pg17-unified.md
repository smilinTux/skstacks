# Migration 3 — PG16 / pgvecto-rs → PG17 unified (skmem-pg)

**Risk: High** (major-version DB upgrade + extension swap). Target image
`skmem-pg:pg17-bm25-age` = one PostgreSQL 17 engine with **pgvector** (vectors) +
**ParadeDB pg_search** (BM25) + **Apache AGE** (graph). v1 ran `pgvector/pgvector:pg16`
plus standalone `pgvecto-rs` — pgvecto-rs is superseded (→ VectorChord / pgvector).

> ⚠️ Apache AGE is the fragile dependency (volunteer-maintained, no PG18 build yet).
> Isolate Cypher behind an interface; keep the recursive-CTE/ltree fallback in mind.

## Precheck
- Record row counts per table and the extensions in use:
  `SELECT extname, extversion FROM pg_extension;`
- Confirm vector dims (skmem-pg uses `vector(1024)`, mxbai embeddings).
- Snapshot which columns are pgvecto-rs vs pgvector indexes (pgvecto-rs uses
  `vectors` ext + its own index ops — these must be rebuilt as pgvector HNSW).

## Backup
```bash
pg_dump -Fc -d <db> -f /backup/pg16-$(date +%F).dump      # custom format
# also keep a plain SQL dump of the schema for index re-creation reference
pg_dump --schema-only -d <db> -f /backup/schema.sql
```

## Restore into PG17 unified
```bash
# 1. start the target engine with the required preloads
docker run -d --name skmem-pg \
  -e POSTGRES_PASSWORD="$PG_SUPERPASS" -v skmem_pgdata:/var/lib/postgresql/data \
  skmem-pg:pg17-bm25-age \
  -c shared_preload_libraries=pg_search,age
# 2. create extensions in the new DB
psql -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql -c "CREATE EXTENSION IF NOT EXISTS pg_search;"
psql -c "CREATE EXTENSION IF NOT EXISTS age;"
# 3. restore data (skip the old 'vectors'/pgvecto-rs extension objects)
pg_restore -d <db> --no-owner /backup/pg16-YYYY-MM-DD.dump
```

## Re-create indexes (the extension swap)
- **Vectors:** drop any pgvecto-rs indexes; create pgvector HNSW:
  ```sql
  CREATE INDEX ON docs USING hnsw (embedding vector_cosine_ops);
  ```
  (Optional upgrade: VectorChord `vchord` is pgvector-data-compatible and ~14–16×
  faster — drop-in if you want it.)
- **BM25:** rebuild the pg_search indexes (`docs_bm25` / `memories_bm25`) with the
  English stemming + stopwords config (see ~/skmem-build/02-enable-bm25-age.sql).
- **Graph:** re-create the AGE graph (`SELECT create_graph('lumina_knowledge');`).
- Re-create the hybrid RRF functions (`hybrid_search_docs/memories`).

## Verify
- Row counts match the precheck per table (docs ≈ 27,314; memories ≈ 15,071).
- Vector recall sanity (`eval_mxbai.py`) + a BM25 `@@@` query return expected hits.
- `ANALYZE;` then check query plans use the HNSW + BM25 indexes.

## Rollback
Keep the PG16 instance + the dump for the rollback window. Apps repoint by DSN, so
reverting is a connection-string change. Cut over app DSNs only after verify passes.
