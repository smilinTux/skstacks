# skcorpus

SKCorpus is a small, read-only OpenAPI tool server (FastAPI) that lets the
skai answer engine search our own knowledge corpora: text chunks in Qdrant
collections (skvector) and, optionally, an entity graph in FalkorDB
(skgraph). A corpus can instead live in a Postgres table or view with
pgvector (`backend: pgvector`, see "pgvector corpora"). skai registers it as
a tool server, so a model can call its operations as tools and cite what it
found.

The code is in this role: [`app/skcorpus.py`](./app/skcorpus.py), one file.
It runs inside a digest-pinned Python image that ships only fastapi, uvicorn
and httpx (and, built from [`image/`](./image/), psycopg for pgvector corpora). The deploy checks the file's
sha256 against the pin in the playbook, copies it to shared storage and
bind-mounts it read-only.

## Tools (OpenAPI operations)

| operationId | Method and path | What it does |
|---|---|---|
| `corpus_list` | `GET /corpora` | The configured corpora: name, description, collections, whether it has a graph |
| `corpus_search` | `POST /search` `{corpus, query, k}` | Embeds `query` (skembed, the corpus's `embed_model`, optional `query_prefix`), searches each collection, merges by score (highest first; ties keep collection order). Returns `text`, `title`, `file_path`, `chunk_index`, `category`, `collection`, `score` |
| `corpus_get` | `POST /chunks` `{corpus, collection, file_path, chunk_index, window}` | The chunks of the same file with `chunk_index` within `window` of the hit, in order |
| `graph_entity` | `POST /graph/entity` `{corpus, name}` | Nodes whose name property equals `name` (case-insensitive): name, labels, properties |
| `graph_neighbors` | `POST /graph/neighbors` `{corpus, name, depth}` | Relations and neighbours of that node, `depth` 1 or 2 |

### One tool server per corpus

Every corpus is served on its own under `/c/<name>/`, with its own spec at
`/c/<name>/openapi.json`. Those operations take no `corpus` argument (the
paths above, minus the field), and the graph tools appear only when the corpus
has a graph. skai registers one tool server per corpus on that prefix
(`server:skcorpus-<name>`), so a model that was given corpus A's tools cannot
reach corpus B, and Open WebUI's access grants on each connection decide who
may use which corpus (see skai's README, `allowed_groups`). The all-corpora
routes in the table, with a `corpus` field, are served only with
`GLOBAL_ROUTES: true` (debugging; off by default).

`GET /health` and `GET /metrics` are unauthenticated (healthcheck and
Prometheus); everything else needs `Authorization: Bearer <API_TOKEN>`.

A collection name in the config may be a Qdrant **alias**. Qdrant resolves
aliases itself, so a corpus that names an alias (for example
`example-chunks`, pointing at `example-chunks-v3`) follows it when the alias
is moved by a nightly rebuild. Prefer aliases over versioned collection names.

### Coherence check

A collection only answers well if every stored vector was made by the model
in `embed_model`. At startup and every `COHERENCE_INTERVAL_SECONDS` (default
3600; 0 = startup only) skcorpus samples `COHERENCE_SAMPLE` points (default
32) per collection with Qdrant's random sampling (`points/query`, read-only;
older Qdrant falls back to the first points of a scroll), re-embeds each
point's `text` (with `document_prefix`, if the corpus was built with one) and
counts the points whose stored vector has cosine above `COHERENCE_MIN_COSINE`
(default 0.99) with the fresh vector. Points without text or vector are
skipped.

- A corpus whose coherent fraction is below its threshold
  (`coherence_threshold` per corpus, else `COHERENCE_THRESHOLD`, default 0.9),
  or whose check failed, is **unhealthy**: `/health` says `"status":
  "degraded"` with the per-corpus ratio, `corpus_list` shows it, and every
  hit it returns carries a `warning` (the preset prompt tells the model to
  say so). It keeps serving: a partly polluted corpus is still useful, and
  hiding it would hide the problem. `/health` stays HTTP 200 so swarm does
  not restart a healthy process over a data problem.
- `/metrics` (Prometheus text): `skcorpus_coherence_ratio{corpus,collection}`
  (collection `""` = the whole corpus), `skcorpus_coherence_healthy{corpus}`,
  `skcorpus_coherence_sampled{corpus}`,
  `skcorpus_coherence_last_check_timestamp_seconds{corpus}`.
- Each check costs `COHERENCE_SAMPLE` embedding calls per collection.

### Read-only by construction

- **Qdrant**: the client's only calls are `POST /collections/<c>/points/search`
  and `.../points/scroll`; every request is checked against that allowlist
  before it is sent. Give it a **read-only** Qdrant API key too
  (`QDRANT_API_KEY`), so the server could not write even if the code changed.
- **FalkorDB**: the only command it can send is `GRAPH.RO_QUERY`, which
  FalkorDB runs read-only (a write clause is an error). The Cypher texts are
  constants in the code; user input (`name`) travels only as a query parameter
  in the `CYPHER name="..."` header, encoded with backslash escapes, never
  pasted into the query. The name property itself (`name_property`) comes from
  the corpus config and must be an identifier.
- **Scope**: a request names a corpus from the config; `corpus_get` must name
  one of that corpus's collections. A collection or graph that is not in the
  config is never touched (404).
- **Limits**: `k` at most `MAX_K` (20), `window` at most `MAX_WINDOW` (3),
  graph rows at most `MAX_GRAPH_ROWS` (50), `depth` 1 or 2, text per chunk cut
  at `MAX_TEXT_CHARS` (4000).

## pgvector corpora

A corpus with `backend: pgvector` reads chunks from a Postgres relation
instead of Qdrant. It has no `collections` and no `graph` (both are refused):
its "collections" are the values of its filter column (`pg_filter_values`),
so hits carry the row's filter value as `collection` and `corpus_get` takes
one of those values.

Expected schema (a table or a view, the name is `pg_relation`):

| Column | Type | Maps to |
|---|---|---|
| `id` | any key type | (internal) |
| `corpus` | text, or the column named by `pg_filter_column` | `collection` |
| `source` | text | `file_path` |
| `chunk_idx` | int | `chunk_index` |
| `content` | text | `text` |
| `meta` | jsonb | `title` and `category` from it |
| `embedding` | `vector(N)`, N = the corpus model's dimension | (search and coherence) |

Hybrid search function (`pg_search_fn`, hybrid vector plus BM25 with RRF):

```sql
fn(q_text text, q_vec vector, k int, agent_filter text DEFAULT NULL,
   rrf_k int DEFAULT 60, vec_w float DEFAULT 2.0)
RETURNS TABLE (id, corpus text, source text, snippet text, vec_rank, bm25_rank, score float)
```

The function does not filter by corpus, so skcorpus asks it for `k * 4` rows
(at most 200), keeps those whose `corpus` is in `pg_filter_values` (best
score first), cuts to `k` and reads their full rows from `pg_relation` by id
(again limited to the filter values). With `pg_search_fn: ""` it searches the
relation by vector distance only (`ORDER BY embedding <=> q_vec`, score
`1 - distance`). `corpus_get` reads the rows of the same `source` and filter
value with `chunk_idx` within the window. The coherence check samples
`COHERENCE_SAMPLE` random rows per filter value (`ORDER BY random()`, so a
full scan of that slice; keep the sample small on large relations) and
compares `embedding` with a fresh embedding of `content`.

Read-only: every connection runs `SET SESSION CHARACTERISTICS AS TRANSACTION
READ ONLY`, `SET default_transaction_read_only = on` and `SET
statement_timeout` (`PG_STATEMENT_TIMEOUT_MS`, default 5000), then checks
`SHOW transaction_read_only` before its first query. The only SQL it can send
is a fixed set of SELECT templates, built once from identifiers that passed
validation (lowercase plain names; `pg_relation` and `pg_search_fn` may be
schema-qualified), with every value a bound parameter. Use a Postgres role
that can only SELECT the relation and EXECUTE the function as well.

The DSN never goes in `corpora.json`: `pg_dsn_env` names an environment
variable (upper case, ending in `_DSN`), and `PG_DSNS` in the vault maps that
name to the DSN, rendered into the 0600 `skcorpus.env`. The default image
has no psycopg, so the play refuses pgvector corpora until `IMAGE` points at
an image built from `image/`. If the database is on an overlay skcorpus does
not join, list it in `PG_NETWORKS` (existing networks, not created here).

```yaml
skcorpus:
  IMAGE: "<registry>/skcorpus-runtime:py3.11-<date>@sha256:<digest>"   # built from image/
  PG_DSNS:
    WIKI_DSN: "postgresql://reader:<password>@<db host>:5432/<db>"
  PG_NETWORKS: [<db overlay>]
  CORPORA:
    - name: example-wiki
      backend: pgvector
      pg_dsn_env: WIKI_DSN
      pg_relation: docs_public                 # default
      pg_search_fn: hybrid_search_docs_public  # default; "" = vector only
      pg_filter_column: corpus                 # default
      pg_filter_values: [wiki, wiki-raw]
      embed_model: example-embed
```

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skcorpus/deploy_skcorpus-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

What the play does:

1. Validates the image pin, the token, the endpoints and the corpora list, and
   checks `app/skcorpus.py` on the controller against `skc_app_sha256` (a
   changed app without a changed pin stops here).
2. Copies the app to `/var/data/skcorpus-<env>/app/skcorpus.py`, renders
   `skcorpus.env` (0600: token and backend credentials) and `corpora.json`
   into `/var/data/config/skcorpus-<env>/`.
3. Ensures the overlays (`skcorpus-<env>`, plus `skembed-<env>`,
   `skvector-<env>` and `skgraph-<env>` with those roles' subnets) and deploys
   stack `skcorpus-<env>`, service `skcorpus`, read-only root filesystem. A
   redeploy that changed the code, the corpora or the env restarts it.

## Configuration (vault)

```yaml
skcorpus:
  API_TOKEN: "..."              # required, 32+ chars (openssl rand -hex 32); same value as skai.CORPUS_TOKEN
  CORPORA:                      # default [] (the server runs and lists nothing)
    - name: example-legal       # plain token; what skai's presets and the tools call it
      backend: qdrant           # optional: qdrant (default) or pgvector (see "pgvector corpora")
      title: "Example Legal"    # optional display name (default: name)
      description: "Example legal corpus"
      collections: [example-chunks, example-reference-v1]   # Qdrant collections or aliases, searched together
      graph: example-graph      # optional FalkorDB graph name
      embed_model: example-embed  # the model the collections were built with (skembed MODEL_NAME)
      embed_url: ""             # optional OpenAI-compatible base URL for this corpus (default EMBED_URL)
      query_prefix: ""          # optional text put before every query (instruction-tuned models)
      document_prefix: ""       # optional text the chunks were embedded with (coherence check)
      allowed_groups: []        # informational here; skai enforces it (skai.CORPORA)
      coherence_threshold: 0.9  # optional, default COHERENCE_THRESHOLD
      name_property: name       # optional node property that holds the entity name (default name)
      vector_name: ""           # optional named vector in the collections (default: the unnamed vector)
  QDRANT_URL: ""                # default http://skvector-<env>_qdrant:6333
  QDRANT_API_KEY: ""            # a READ-ONLY key
  FALKORDB_HOST: ""             # default skgraph-<env>_falkordb
  FALKORDB_PORT: 6379
  FALKORDB_USERNAME: ""         # an ACL user limited to GRAPH.RO_QUERY, if the instance uses ACLs
  FALKORDB_PASSWORD: ""
  EMBED_URL: ""                 # default http://skembed-<env>_skembed:8080/v1; set it to use an existing endpoint (no skembed)
  EMBED_API_KEY: ""             # same value as skembed.API_KEY, if set
  PG_DSNS: {}                   # pgvector corpora: {NAME_DSN: "postgresql://..."}, secret, env file only
  PG_NETWORKS: []               # existing overlays to join for the pgvector databases
  PG_STATEMENT_TIMEOUT_MS: 5000 # per-statement limit on Postgres
  GLOBAL_ROUTES: false          # true also serves the all-corpora routes
  COHERENCE_SAMPLE: 32          # points sampled per collection per check
  COHERENCE_INTERVAL_SECONDS: 3600   # 0 = check at startup only
  COHERENCE_MIN_COSINE: 0.99    # a point is coherent above this cosine
  COHERENCE_THRESHOLD: 0.9      # coherent fraction a corpus needs to be healthy
  MAX_K: 20
  MAX_WINDOW: 3
  MAX_GRAPH_ROWS: 50
  MAX_TEXT_CHARS: 4000
  IMAGE: ""                     # default below; must carry @sha256:
  REPLICAS: 1
  CPUS: "1.0"
  MEMORY_LIMIT: 512M
  MEMORY_RESERVATION: 64M
  PLACEMENT_CONSTRAINTS: []     # default ["node.role == worker"]
  CONNECT_NETWORKS: []          # default [skembed-<env> (only when EMBED_URL is empty), skvector-<env>, skgraph-<env>]
  networks: []                  # override the overlays to create (subnet registry)
```

| Key | Default | Notes |
|---|---|---|
| `API_TOKEN` | none (required) | Secret. skai sends it as the tool server key (`skai.CORPUS_TOKEN`). |
| `CORPORA` | `[]` | Configuration, not input: the model can only pick among these names. The payload of each chunk is expected to carry `text`, `title`, `file_path`, `chunk_index` (and optionally `category`). |
| `embed_model` | none (required per corpus) | Must be the model that built the collections, or search returns noise. Check with one stored chunk: embed its text and compare with its stored vector (cosine near 1.0). |
| `QDRANT_API_KEY` | none | Secret. Use Qdrant's read-only key (`read_only_api_key`), not the admin key. |
| `FALKORDB_PASSWORD` | none | Secret. |
| `PG_DSNS` | `{}` | Secret. One entry per `pg_dsn_env` of a pgvector corpus (key upper case ending in `_DSN`, value a one-line DSN of a read-only role). Rendered only into `skcorpus.env`. |
| `PG_NETWORKS` | `[]` | Added to the networks skcorpus joins; they must already exist. |
| `PG_STATEMENT_TIMEOUT_MS` | 5000 | `statement_timeout` of every Postgres session. |
| `IMAGE` | `smilintux/skpayment-runtime:py3.11-20261123@sha256:79bd0222...` | The published runtime with exactly the packages in `image/requirements.txt` (Python 3.11 slim, fastapi, uvicorn, httpx). Build `image/` and pin your own tag to replace it. |
| `EMBED_URL` | skembed of the same env | Any OpenAI-compatible `/v1` base (`POST <url>/embeddings`). When set, skembed is optional: its overlay is neither joined nor created. A corpus's `embed_url` overrides it for that corpus. |
| `COHERENCE_*` | 32, 3600, 0.99, 0.9 | See "Coherence check". |
| `CONNECT_NETWORKS` | skembed (unless `EMBED_URL` is set), skvector, skgraph of the same env | Override when the backends live elsewhere (then set the URLs too). |

## Networks

| Network | Subnet |
|---|---|
| `skcorpus-prod` / `-staging` / `-dev` | `172.16.123.0/24` / `172.16.124.0/24` / `172.16.125.0/24` |
| joins | `skvector-<env>`, `skgraph-<env>`, and `skembed-<env>` unless `EMBED_URL` is set (subnets from those roles) |

## Instance actions

- Generate `API_TOKEN` and set the same value as `skai.CORPUS_TOKEN`.
- Create a read-only Qdrant key and set `QDRANT_API_KEY`; set the FalkorDB password.
- List the corpora, and check each corpus's `embed_model` against its collections (see the table).
- Point each collection at an alias where the collection is rebuilt, and set
  `embed_url` per corpus when its model is served elsewhere (then skembed is
  not needed).
- Deploy skembed (if used), then skcorpus, then skai. Watch the first
  coherence result: `curl -s http://skcorpus-<env>_skcorpus:8000/health`.

## Verification

```bash
docker service ls --filter name=skcorpus
# from a container on the skcorpus-<env> network:
curl -s http://skcorpus-<env>_skcorpus:8000/health          # status, coherence per corpus
curl -s http://skcorpus-<env>_skcorpus:8000/metrics
curl -s -H "Authorization: Bearer $TOKEN" http://skcorpus-<env>_skcorpus:8000/c/example-legal/corpora
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  http://skcorpus-<env>_skcorpus:8000/c/example-legal/search -d '{"query":"notice period","k":3}'
```

Tests: `v1/tests/test_skcorpus_app.py` (the app against a fake Qdrant, a fake
embeddings endpoint, a fake FalkorDB and a fake Postgres connection, no
network: read-only guarantee (read-only Postgres session, SQL templates only),
Cypher parameterization with an injection attempt, merge ordering, limits,
empty config, auth, per-corpus servers, per-corpus embed_url, the coherence
check against a fake Qdrant with polluted points, pgvector config validation,
hybrid over-fetch and filtering, the vector-only fallback, the window and
coherence on pgvector samples) and `v1/tests/test_skcorpus_render.py` (the deploy through
real Ansible: app pin, corpora validation, networks, secrets kept out of the
compose file).
