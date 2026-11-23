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

`GET /health` is unauthenticated (the healthcheck); everything else needs
`Authorization: Bearer <API_TOKEN>`. `GET /openapi.json` is the spec skai reads.

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
      description: "Example legal corpus"
      collections: [example-chunks-v1, example-reference-v1]   # Qdrant collections, searched together
      graph: example-graph      # optional FalkorDB graph name
      embed_model: example-embed  # the model the collections were built with (skembed MODEL_NAME)
      query_prefix: ""          # optional text put before every query (instruction-tuned models)
      name_property: name       # optional node property that holds the entity name (default name)
      vector_name: ""           # optional named vector in the collections (default: the unnamed vector)
  QDRANT_URL: ""                # default http://skvector-<env>_qdrant:6333
  QDRANT_API_KEY: ""            # a READ-ONLY key
  FALKORDB_HOST: ""             # default skgraph-<env>_falkordb
  FALKORDB_PORT: 6379
  FALKORDB_USERNAME: ""         # an ACL user limited to GRAPH.RO_QUERY, if the instance uses ACLs
  FALKORDB_PASSWORD: ""
  EMBED_URL: ""                 # default http://skembed-<env>_skembed:8080/v1
  EMBED_API_KEY: ""             # same value as skembed.API_KEY, if set
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
  CONNECT_NETWORKS: []          # default [skembed-<env>, skvector-<env>, skgraph-<env>]
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
| `CONNECT_NETWORKS` | skembed, skvector, skgraph of the same env | Override when the backends live elsewhere (then set the URLs too). |

## Networks

| Network | Subnet |
|---|---|
| `skcorpus-prod` / `-staging` / `-dev` | `172.16.123.0/24` / `172.16.124.0/24` / `172.16.125.0/24` |
| joins | `skembed-<env>`, `skvector-<env>`, `skgraph-<env>` (subnets from those roles) |

## Instance actions

- Generate `API_TOKEN` and set the same value as `skai.CORPUS_TOKEN`.
- Create a read-only Qdrant key and set `QDRANT_API_KEY`; set the FalkorDB password.
- List the corpora, and check each corpus's `embed_model` against its collections (see the table).
- Deploy skembed, then skcorpus, then skai.

## Verification

```bash
docker service ls --filter name=skcorpus
# from a container on the skcorpus-<env> network:
curl -s http://skcorpus-<env>_skcorpus:8000/health
curl -s -H "Authorization: Bearer $TOKEN" http://skcorpus-<env>_skcorpus:8000/corpora
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  http://skcorpus-<env>_skcorpus:8000/search -d '{"corpus":"example-legal","query":"notice period","k":3}'
```

Tests: `v1/tests/test_skcorpus_app.py` (the app against a fake Qdrant, a fake
embeddings endpoint and a fake FalkorDB, no network: read-only guarantee,
Cypher parameterization with an injection attempt, merge ordering, limits,
empty config, auth) and `v1/tests/test_skcorpus_render.py` (the deploy through
real Ansible: app pin, corpora validation, networks, secrets kept out of the
compose file).
