# skcorpus

SKCorpus is a small, read-only OpenAPI tool server (FastAPI) that lets the
skai answer engine search our own knowledge corpora: text chunks in Qdrant
collections (skvector) and, optionally, an entity graph in FalkorDB
(skgraph). skai registers it as a tool server, so a model can call its
operations as tools and cite what it found.

The code is in this role: [`app/skcorpus.py`](./app/skcorpus.py), one file.
It runs inside a digest-pinned Python image that ships only fastapi, uvicorn
and httpx (built from [`image/`](./image/)). The deploy checks the file's
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
embeddings endpoint and a fake FalkorDB, no network: read-only guarantee,
Cypher parameterization with an injection attempt, merge ordering, limits,
empty config, auth, per-corpus servers, per-corpus embed_url, the coherence
check against a fake Qdrant with polluted points) and `v1/tests/test_skcorpus_render.py` (the deploy through
real Ansible: app pin, corpora validation, networks, secrets kept out of the
compose file).
