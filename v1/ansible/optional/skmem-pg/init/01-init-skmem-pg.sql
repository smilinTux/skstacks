-- skmem-pg init — runs once on a fresh data volume (/docker-entrypoint-initdb.d).
-- Requires the container to start with: -c shared_preload_libraries=pg_search,age
-- Produces a ready skmemory store: pgvector + pg_search(BM25, stemming) + AGE + hybrid fns.
\set ON_ERROR_STOP on

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_search;
CREATE EXTENSION IF NOT EXISTS age;

-- ---- canonical schema (embedding = mxbai-embed-large, 1024-dim) ----
CREATE TABLE IF NOT EXISTS docs (
  id          bigserial PRIMARY KEY,
  corpus      text,
  source      text,
  chunk_idx   integer,
  content     text,
  meta        jsonb DEFAULT '{}'::jsonb,
  embedding   vector(1024),                 -- mxbai-embed-large
  emb_bge_legal vector(1024),               -- optional legacy/legal backup column
  agent       text DEFAULT 'default',
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', COALESCE(content,''))) STORED
);
CREATE TABLE IF NOT EXISTS memories (
  id          text PRIMARY KEY,
  layer       text,
  role        text,
  title       text,
  content     text,
  summary     text,
  tags        text[] DEFAULT '{}',
  source      text,
  created_at  timestamptz DEFAULT now(),
  updated_at  timestamptz DEFAULT now(),
  memory_json jsonb NOT NULL,
  embedding   vector(1024),
  emb_bge_legal vector(1024),
  agent       text DEFAULT 'default',
  tsv         tsvector GENERATED ALWAYS AS (
                to_tsvector('english', COALESCE(title,'')||' '||COALESCE(content,'')||' '||COALESCE(summary,''))) STORED
);

-- ---- vector (HNSW) ----
CREATE INDEX IF NOT EXISTS docs_hnsw     ON docs     USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS memories_hnsw ON memories USING hnsw (embedding vector_cosine_ops);

-- ---- BM25 (pg_search) with English stemming + stopwords ----
CREATE INDEX IF NOT EXISTS docs_bm25 ON docs USING bm25 (id, content, corpus, source)
  WITH (key_field='id', text_fields='{"content":{"tokenizer":{"type":"default","stemmer":"English","stopwords_language":"English"}},"corpus":{"tokenizer":{"type":"default","stemmer":"English"}},"source":{"tokenizer":{"type":"default"}}}');
CREATE INDEX IF NOT EXISTS memories_bm25 ON memories USING bm25 (id, title, content, summary)
  WITH (key_field='id', text_fields='{"title":{"tokenizer":{"type":"default","stemmer":"English","stopwords_language":"English"}},"content":{"tokenizer":{"type":"default","stemmer":"English","stopwords_language":"English"}},"summary":{"tokenizer":{"type":"default","stemmer":"English","stopwords_language":"English"}}}');
CREATE INDEX IF NOT EXISTS docs_agent     ON docs(agent);
CREATE INDEX IF NOT EXISTS memories_agent ON memories(agent);

-- ---- hybrid search (vector + BM25, RRF; vector weighted 2x) ----
CREATE OR REPLACE FUNCTION hybrid_search_docs(q_text text, q_vec vector(1024), k int DEFAULT 10, agent_filter text DEFAULT NULL, rrf_k int DEFAULT 60, vec_w float DEFAULT 2.0)
RETURNS TABLE(id bigint, corpus text, source text, content text, vec_rank int, bm25_rank int, score float) AS $$
WITH vec AS (SELECT d.id, row_number() OVER (ORDER BY d.embedding <=> q_vec) r FROM docs d
             WHERE q_vec IS NOT NULL AND d.embedding IS NOT NULL AND (agent_filter IS NULL OR d.agent=agent_filter)
             ORDER BY d.embedding <=> q_vec LIMIT 100),
     bm AS (SELECT d.id, row_number() OVER (ORDER BY paradedb.score(d.id) DESC) r FROM docs d
            WHERE d.content @@@ q_text AND (agent_filter IS NULL OR d.agent=agent_filter)
            ORDER BY paradedb.score(d.id) DESC LIMIT 100)
SELECT d.id,d.corpus,d.source,left(d.content,160),vec.r::int,bm.r::int,
       (vec_w*COALESCE(1.0/(rrf_k+vec.r),0)+COALESCE(1.0/(rrf_k+bm.r),0))::float
FROM docs d LEFT JOIN vec ON vec.id=d.id LEFT JOIN bm ON bm.id=d.id
WHERE vec.id IS NOT NULL OR bm.id IS NOT NULL ORDER BY 7 DESC LIMIT k;
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION hybrid_search_memories(q_text text, q_vec vector(1024), k int DEFAULT 10, agent_filter text DEFAULT NULL, rrf_k int DEFAULT 60, vec_w float DEFAULT 2.0)
RETURNS TABLE(id text, layer text, title text, content text, vec_rank int, bm25_rank int, score float) AS $$
WITH vec AS (SELECT m.id, row_number() OVER (ORDER BY m.embedding <=> q_vec) r FROM memories m
             WHERE q_vec IS NOT NULL AND m.embedding IS NOT NULL AND (agent_filter IS NULL OR m.agent=agent_filter)
             ORDER BY m.embedding <=> q_vec LIMIT 100),
     bm AS (SELECT m.id, row_number() OVER (ORDER BY paradedb.score(m.id) DESC) r FROM memories m
            WHERE m.content @@@ q_text AND (agent_filter IS NULL OR m.agent=agent_filter)
            ORDER BY paradedb.score(m.id) DESC LIMIT 100)
SELECT m.id,m.layer,m.title,left(m.content,160),vec.r::int,bm.r::int,
       (vec_w*COALESCE(1.0/(rrf_k+vec.r),0)+COALESCE(1.0/(rrf_k+bm.r),0))::float
FROM memories m LEFT JOIN vec ON vec.id=m.id LEFT JOIN bm ON bm.id=m.id
WHERE vec.id IS NOT NULL OR bm.id IS NOT NULL ORDER BY 7 DESC LIMIT k;
$$ LANGUAGE sql STABLE;

-- ---- AGE graph (per-agent knowledge graph) ----
LOAD 'age'; SET search_path = ag_catalog, "$user", public;
SELECT create_graph('agent_knowledge');

ALTER DATABASE skmemory SET paradedb.check_topk_scan = false;
