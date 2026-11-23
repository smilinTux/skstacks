# skai

SKAI is the estate's answer engine: fast chat with any model behind an
OpenAI-compatible gateway, answers that search the web and cite their
sources, and a knowledge base that searches our own corpora (vectors and an
entity graph) and cites the file and chunk it used. It is
[Open WebUI](https://github.com/open-webui/open-webui) at a pinned release,
configured purely through environment variables, plus two companion roles:

- [`skembed`](../skembed/README.md): query and document embeddings on CPU (`/v1/embeddings`).
- [`skcorpus`](../skcorpus/README.md): the read-only corpus search tool server.

```mermaid
flowchart LR
  B[browser] --> T[Traefik] --> A[Authentik OIDC] --> W[skai: Open WebUI]
  W -->|chat + task models| G[OpenAI-compatible gateway]
  W -->|web search| P[skpeek: SearXNG]
  W -->|uploads, fetched pages| E[skembed] & D[(skai db: pgvector)]
  W -->|tool server: corpus_*| C[skcorpus]
  C --> E
  C -->|search, scroll| Q[(skvector: Qdrant)]
  C -->|GRAPH.RO_QUERY| F[(skgraph: FalkorDB)]
```

No Ollama and no local model runtime: model routing is the gateway's job.

## What is in the stack

| Service | Image (default, pinned by index digest) | Notes |
|---|---|---|
| `skai` | `ghcr.io/open-webui/open-webui:v0.13.4@sha256:d5168bfb...` | One replica, port 8080, data in `DATA_PATH` (uploads, the local reranker model) |
| `db` | `pgvector/pgvector:0.8.7-pg17-bookworm@sha256:b513c485...` | App data AND upload/web vectors (`VECTOR_DB=pgvector`), stop-first |
| `db-backup` | the same image | `pg_dumpall` loop: unique names, temp file renamed on success, prune after a good dump only |
| `voice` (with `VOICE: true`) | `ghcr.io/speaches-ai/speaches:0.8.3-cpu@sha256:21e3df06...` | faster-whisper STT and Piper TTS, OpenAI-compatible, API key |

## How it is configured

Everything is in `/var/data/config/skai-<env>/skai.env` (0600), rendered from
the vault on every deploy, with `ENABLE_PERSISTENT_CONFIG=false`: Open WebUI
takes its settings from the environment on every start and no setup wizard
state exists. A setting changed in the admin UI lasts until the next restart;
make it a vault key instead. Env names were checked against the pinned
release's `backend/open_webui/config.py` and `env.py` (v0.13.4).

| Area | Open WebUI env (what the role sets) |
|---|---|
| Models | `ENABLE_OLLAMA_API=false`, `OPENAI_API_BASE_URL` / `OPENAI_API_KEY` (gateway), `DEFAULT_MODELS` (`DEFAULT_MODEL`), `TASK_MODEL_EXTERNAL` and `TASK_MODEL` (`TASK_MODEL`: titles, tags, follow-ups, search query generation) |
| Web search | `ENABLE_WEB_SEARCH`, `WEB_SEARCH_ENGINE=searxng`, `SEARXNG_QUERY_URL`, `ENABLE_SEARCH_QUERY_GENERATION=true`, `WEB_SEARCH_RESULT_COUNT` (6), `WEB_SEARCH_CONCURRENT_REQUESTS` (4), `WEB_LOADER_CONCURRENT_REQUESTS` (6), `BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL=false` (fetched pages are chunked, embedded and reranked, not pasted whole) |
| Retrieval | `RAG_EMBEDDING_ENGINE=openai`, `RAG_OPENAI_API_BASE_URL` / `RAG_OPENAI_API_KEY` / `RAG_EMBEDDING_MODEL` (skembed), `VECTOR_DB=pgvector`, `PGVECTOR_DB_URL`, `ENABLE_RAG_HYBRID_SEARCH=true`, `RAG_RERANKING_MODEL`, optional `RAG_RERANKING_ENGINE=external` + `RAG_EXTERNAL_RERANKER_URL`, `RAG_TOP_K`, `RAG_TOP_K_RERANKER`, `ENABLE_RETRIEVAL_QUERY_GENERATION=true` |
| Tool server | `ENABLE_TOOL_SERVERS=true`, `TOOL_SERVER_CONNECTIONS` (below) |
| Sign-in | `ENABLE_OAUTH_SIGNUP`, `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET`, `OPENID_PROVIDER_URL`, `OPENID_REDIRECT_URI`, `OAUTH_PROVIDER_NAME`, `OAUTH_SCOPES`, `ENABLE_OAUTH_ROLE_MANAGEMENT=true`, `OAUTH_ROLES_CLAIM=groups`, `OAUTH_ADMIN_ROLES`, `OAUTH_ALLOWED_ROLES`, `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=false`, `ENABLE_LOGIN_FORM=false`, `ENABLE_SIGNUP=false` |
| Bootstrap | `WEBUI_ADMIN_EMAIL` / `WEBUI_ADMIN_PASSWORD` (the provisioner admin, created on the first start only), `WEBUI_SECRET_KEY`, `DATABASE_URL`, `WEBUI_URL` |
| Voice | `AUDIO_STT_ENGINE=openai`, `AUDIO_STT_OPENAI_API_BASE_URL` / `_KEY`, `AUDIO_STT_MODEL`, `AUDIO_TTS_ENGINE=openai`, `AUDIO_TTS_OPENAI_API_BASE_URL` / `_KEY`, `AUDIO_TTS_MODEL`, `AUDIO_TTS_VOICE` |

### The corpus tool server (`TOOL_SERVER_CONNECTIONS`)

With `CORPUS_TOKEN` set, the env carries one OpenAPI tool server (the v0.13
`ToolServerConnection` shape, `backend/open_webui/routers/configs.py`):

```json
[{"url": "http://skcorpus-<env>_skcorpus:8000", "path": "openapi.json", "type": "openapi",
  "auth_type": "bearer", "key": "<CORPUS_TOKEN>",
  "config": {"enable": true, "access_grants": [{"principal_type": "user", "principal_id": "*", "permission": "read"}]},
  "info": {"id": "skcorpus", "name": "Corpus search", "description": "..."}}]
```

`access_grants` with the `*` user makes the tools usable by every signed-in
user (without it a tool server is admin-only). Models reference it as tool id
`server:skcorpus`.

### Corpus presets

For each entry in `CORPORA` the deploy creates or updates one Open WebUI
model preset, id `corpus-<name>`: the base model (`base_model`, default
`DEFAULT_MODEL`), the `server:skcorpus` tool, native function calling, and a
system prompt that answers from the corpus in layers: the graph
(`graph_entity`, `graph_neighbors`) for who or what connects to what, vectors
(`corpus_search`) for substance, `corpus_get` for context, chunk text quoted
word for word only when asked, and every source cited as title, file_path and
chunk_index. `system_prompt` replaces the default per corpus (`{corpus}` and
`{title}` are filled in). The presets are readable by every user.

How: there is no env for model presets, so the deploy renders
`presets.json` (no secrets) and runs `app/provision_presets.py` inside the
skai container (`docker exec`, standard library Python). It signs in on
`127.0.0.1:8080` as the provisioner admin (credentials from the container
env, never on a command line) and uses the app's own API: create a missing
preset, update a changed one, leave the rest alone. It never deletes a model;
remove a retired preset in the admin UI.

## Sign-in (Authentik OIDC)

`AUTH_MODE: oidc` (the default) needs the provider on the Authentik side:
`sksso.SKAI_OIDC_ENABLED: true` and the same secret in both vaults (see
`../sksso/README.md` "skai (Open WebUI) OIDC login"). Deploy sksso first.

- **One Application.** The portal tile slug equals the OIDC client id
  (`skai`), so the tile and the provider are one Authentik Application; who
  may sign in is the tile's group binding, `AUTHENTIK_TILE_GROUP` (default
  `skadmin`). Widen it for everyone who should use skai.
- **Admins by group.** `ADMIN_GROUP` (default `skadmin`) members are made
  Open WebUI admins on every login (the `groups` claim of the profile scope);
  everyone else allowed in is a user. `USER_GROUPS` (default `["*"]`: anyone
  Authentik lets through) narrows that further inside Open WebUI.
- **Password sign-in.** The login form is hidden and sign-up is off. Password
  sign-in stays on in the app only for the provisioner, and Traefik answers
  403 to `/api/v1/auths/signin` and `/signup` from outside (an `ipAllowList`
  of `127.0.0.1/32` on a priority-1000 router).
- **The provisioner account** is the first user (created from env on the
  first start). Because it exists, the first OIDC user is NOT made admin
  automatically: admin rights come only from `ADMIN_GROUP`. A database that
  already has users (an import) gets no provisioner account: create the
  account by hand with that email and password, as admin, or presets fail.

`AUTH_MODE: local` shows the password form and needs no sksso change (labs).

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skai/deploy_skai-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

Order on an estate: skpeek, skembed, skcorpus, sksso (with `SKAI_OIDC_ENABLED`), skai.

What the play does: validates pins, secrets and knobs (fails closed); renders
`skai.env`, `postgres.env`, `postgres-backup.env` and `voice.env` (all 0600),
`presets.json` and the compose file; ensures the overlays (`skai-<env>`,
`cloud-public-<env>`, `skpeek-<env>`, `skembed-<env>`, `skcorpus-<env>`);
deploys stack `skai-<env>` (a changed env restarts the app service); with
corpora configured, waits for the running task and provisions the presets;
creates or updates the portal tile.

## Configuration (vault)

```yaml
skai:
  CLUSTERNAME: skstack01            # default: inventory cluster_name
  DOMAIN: example.com               # default: inventory domain
  CLOUDFLARED: false                # true: default host is under DOMAIN, not CLUSTERNAME.DOMAIN
  HOSTNAME: ""                      # default skai[-dev|-staging].<base domain>
  # secrets (required)
  SECRET_KEY: "..."                 # 32+ chars, WEBUI_SECRET_KEY (sessions, encrypted fields)
  DB_PASSWORD: "..."                # 16+ chars of [A-Za-z0-9._~-]
  PROVISIONER_PASSWORD: "..."       # 32+ chars, the env-created admin the deploy uses for presets
  GATEWAY_API_KEY: "..."            # the gateway key
  OIDC_CLIENT_SECRET: "..."         # 32+ chars with AUTH_MODE oidc; same value as sksso.SKAI_OIDC_CLIENT_SECRET
  # models
  GATEWAY_URL: "https://gateway.example.com/v1"   # required, OpenAI-compatible base URL
  DEFAULT_MODEL: ""                 # the default chat model id (required with CORPORA without base_model)
  TASK_MODEL: ""                    # a fast model for titles and search queries; default DEFAULT_MODEL
  # web search
  WEB_SEARCH: true
  SEARXNG_URL: ""                   # default http://skpeek-<env>_skpeek:8080/search?q=<query>
  WEB_SEARCH_RESULT_COUNT: 6
  WEB_SEARCH_CONCURRENT_REQUESTS: 4
  WEB_LOADER_CONCURRENT_REQUESTS: 6
  # retrieval
  EMBED_URL: ""                     # default http://skembed-<env>_skembed:8080/v1
  EMBED_MODEL: ""                   # required: skembed.MODEL_NAME
  EMBED_API_KEY: ""                 # skembed.API_KEY, if set
  RERANK_MODEL: ""                  # default BAAI/bge-reranker-v2-m3 (local, downloaded into DATA_PATH)
  RERANK_URL: ""                    # optional external reranker endpoint instead of the local model
  RERANK_API_KEY: ""
  RAG_TOP_K: 8
  RAG_TOP_K_RERANKER: 5
  # corpus tool server and presets
  CORPUS_URL: ""                    # default http://skcorpus-<env>_skcorpus:8000
  CORPUS_TOKEN: ""                  # 32+ chars, same value as skcorpus.API_TOKEN; empty = no tool server
  CORPUS_TOOL_NAME: ""              # default "Corpus search"
  CORPORA: []                       # presets: [{name, title, description, base_model, system_prompt}]
  # sign-in
  AUTH_MODE: oidc                   # oidc (default) or local
  ADMIN_GROUP: skadmin              # Authentik group whose members are Open WebUI admins
  USER_GROUPS: ["*"]                # groups allowed in by Open WebUI ("*": whoever Authentik lets through)
  OIDC_CLIENT_ID: ""                # default AUTHENTIK_TILE_SLUG, else skai
  OIDC_SSO_HOST: ""                 # default sso[-<env>].<cluster>.<domain>
  OIDC_DISCOVERY_URL: ""            # full override
  OIDC_PROVIDER_NAME: ""            # login button label, default "SKStack SSO"
  PROVISIONER_EMAIL: ""             # default skai-provisioner@<DOMAIN>
  DEFAULT_USER_ROLE: user           # role of a new user that no group maps (user or pending)
  WEBUI_NAME: SKAI
  # voice (optional)
  VOICE: false
  VOICE_API_KEY: ""                 # 16+ chars with VOICE
  VOICE_STT_MODEL: ""               # default Systran/faster-whisper-small
  VOICE_TTS_MODEL: ""               # default speaches-ai/piper-en_US-amy-medium
  VOICE_TTS_VOICE: ""               # default amy
  # images, storage, placement, routing
  IMAGE: ""                         # each must carry @sha256:
  DB_IMAGE: ""
  VOICE_IMAGE: ""
  DATA_PATH: ""                     # default /var/data/skai-<env>/data
  DB_PATH: ""                       # default /var/data/skai-<env>/postgres
  DUMP_PATH: ""                     # default /var/data/skai-<env>/dump
  VOICE_PATH: ""                    # default /var/data/skai-<env>/voice
  BACKUP_KEEP_DAYS: 7
  BACKUP_FREQUENCY: 86400
  PLACEMENT_CONSTRAINTS: []         # app and voice; default ["node.role == worker"]
  DB_PLACEMENT_CONSTRAINTS: []      # db and db-backup; default ["node.role == worker"]
  CONNECT_NETWORKS: []              # default [skpeek-<env>, skembed-<env>, skcorpus-<env>]
  CPUS: "2.0"
  MEMORY_LIMIT: 4G
  MEMORY_RESERVATION: 1G
  POSTGRES_CPUS: "1.00"
  POSTGRES_MEMORY_LIMIT: 2048M
  POSTGRES_MEMORY_RESERVATION: 256M
  POSTGRES_HEALTH_START_PERIOD: 300s
  VOICE_CPUS: "2.0"
  VOICE_MEMORY_LIMIT: 3G
  VOICE_MEMORY_RESERVATION: 512M
  HEALTH_START_PERIOD: 600s
  FIRST_START_WAIT_SECONDS: 900
  MIDDLEWARES: ""                   # default skai-chain@file (microphone/camera allowed, SAMEORIGIN)
  API_MIDDLEWARES: ""               # /api/, /oauth/, /ws/ router; default derived (no error pages)
  TLS_OPTIONS: ""                   # default none (Traefik's default TLS options)
  AUTHENTIK_TILE: true
  AUTHENTIK_TILE_SLUG: ""           # default skai (= the OIDC client id)
  AUTHENTIK_TILE_NAME: ""           # default SKAI
  AUTHENTIK_TILE_GROUP: ""          # default skadmin: who may sign in
  AUTHENTIK_TILE_ICON: ""           # default fa://fa-robot
  AUTHENTIK_TILE_OPEN_IN_NEW_TAB: true
  AUTHENTIK_TILE_DESCRIPTION: ""
  networks: []                      # override the overlays to create (subnet registry)
```

Secrets: `SECRET_KEY`, `DB_PASSWORD`, `PROVISIONER_PASSWORD`, `GATEWAY_API_KEY`,
`OIDC_CLIENT_SECRET`, `CORPUS_TOKEN`, `EMBED_API_KEY`, `RERANK_API_KEY`,
`VOICE_API_KEY`. They are only written to the 0600 env files; `env_file`
values are copied into the swarm service spec, so never print a service
spec's env.

## Networks

| Network | Subnet |
|---|---|
| `skai-prod` / `-staging` / `-dev` | `172.16.120.0/24` / `172.16.121.0/24` / `172.16.122.0/24` |
| joins | `cloud-public-<env>`, `skpeek-<env>`, `skembed-<env>`, `skcorpus-<env>` |

## Instance actions

1. Generate `SECRET_KEY`, `DB_PASSWORD`, `PROVISIONER_PASSWORD` and `OIDC_CLIENT_SECRET`
   (`openssl rand -hex 32`); put the OIDC secret in the sksso vault too
   (`SKAI_OIDC_ENABLED: true`, `SKAI_OIDC_CLIENT_SECRET`) and redeploy sksso.
2. Set `GATEWAY_URL`, `GATEWAY_API_KEY`, `DEFAULT_MODEL` and `TASK_MODEL`.
3. Deploy skembed and set `EMBED_MODEL` to its `MODEL_NAME`.
4. For the knowledge base: deploy skcorpus, set `CORPUS_TOKEN` (= `skcorpus.API_TOKEN`) and
   `CORPORA` (one entry per skcorpus corpus that should get a preset).
5. Set `AUTHENTIK_TILE_GROUP` (who may sign in) and `ADMIN_GROUP` (who administers).
6. Voice: `VOICE: true`, `VOICE_API_KEY`, then download the models once inside the voice
   container (`curl -X POST -H "Authorization: Bearer $KEY" localhost:8000/v1/models/<id>`
   for the STT and TTS model ids), and check the TTS voice name the model lists.

## Verification

```bash
docker service ls --filter name=skai
curl -sI https://<HOSTNAME>/ | head -1                       # 200 (or the SSO redirect)
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<HOSTNAME>/api/v1/auths/signin   # 403 in oidc mode
```

Tests: `v1/tests/test_skai_render.py` (the deploy through real Ansible:
pins, fail-closed secrets and knobs, the env names, the tool server JSON, the
presets, the routers), `v1/tests/test_skai_provision.py` (the preset
provisioner against a fake Open WebUI API) and
`v1/tests/test_skai_oidc_sksso.py` (the sksso blueprint and its byte-identity
when off).

## docs: full-gate stage plan (release harness)

What a lab stage in the full release gate should check for these three roles.
The lab has no real corpus, so the stage seeds its own tiny fixture. Nothing
here is in the harness yet; this is the plan for it.

1. **skembed answers.** Deploy skembed with a small public model (`MODEL_ID`
   of a MiniLM-sized sentence-transformers model, `MODEL_NAME: lab-embed`).
   Wait for the task to be healthy, then from a container on `skembed-<env>`:
   `POST /v1/embeddings {"model":"lab-embed","input":["hello"]}` returns 200
   with one vector of the model's dimension; a second call with two inputs
   returns two vectors.
2. **skcorpus searches a fixture it seeds itself.** Deploy skvector (and
   skgraph) if the lab does not already run them. The stage creates a Qdrant
   collection `lab-fixture-v1` (cosine, the model's dimension) with three
   points whose payload carries `text`, `title`, `file_path`, `chunk_index`
   (two chunks of one file, one of another), embedding each text through
   skembed; and a FalkorDB graph `lab-fixture` with two nodes joined by one
   relation (`CREATE` through the admin connection, never through skcorpus).
   Deploy skcorpus with one corpus `{name: lab-fixture, collections:
   [lab-fixture-v1], graph: lab-fixture, embed_model: lab-embed}`. Check:
   `/health` 200; `/corpora` without a token 401, with it lists `lab-fixture`;
   `corpus_search` for the text of chunk 0 returns that chunk first with
   title, file_path and chunk_index; `corpus_get` with `window: 1` returns
   chunks 0 and 1 in order; `graph_neighbors` of the first node returns the
   second; a `graph_entity` name containing `") DETACH DELETE n //` returns
   200 with no rows and the graph still has both nodes afterwards.
3. **skai serves its login page and has the tool server.** Deploy sksso with
   `SKAI_OIDC_ENABLED`, then skai with `CORPUS_TOKEN` and `CORPORA:
   [{name: lab-fixture}]`, the lab gateway (or any OpenAI-compatible stub) as
   `GATEWAY_URL`. Check: `GET https://<skai host>/health` 200 through
   skfenceha; the root page serves the Open WebUI app and
   `/api/config` shows the OIDC provider (`oauth.providers.oidc`); `POST
   /api/v1/auths/signin` from outside answers 403; inside the container, the
   provisioner admin's token reads `GET /api/v1/configs/tool_servers` with
   one connection whose `info.id` is `skcorpus`, `GET /api/v1/models/model?id=corpus-lab-fixture`
   returns the preset with `server:skcorpus` in `meta.toolIds`, and a second
   deploy reports `PRESET_UNCHANGED corpus-lab-fixture`. The Authentik API
   shows the `skai` Application with the OAuth2 provider, the group binding
   and the icon.
4. **Teardown** removes the fixture collection and graph.
