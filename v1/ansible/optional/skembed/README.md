# skembed

SKEmbed serves one text embedding model on CPU as a swarm service with an
OpenAI-compatible `POST /v1/embeddings`. It exists so that query embeddings
for retrieval (skcorpus) and for uploaded documents (skai) do not need a GPU,
and so that the model that built a vector collection is the same one that
embeds the queries against it. A single query on CPU takes tens to a few
hundred milliseconds, which is fine for chat.

Internal only: there is no Traefik router and no web UI (so no portal tile).
Consumers on the same estate attach to the `skembed-<env>` overlay and call
`http://skembed-<env>_skembed:8080/v1`. Something on another host (an LLM
gateway that should offer the model as an embeddings route) can use the
optional published port.

## Why text-embeddings-inference

| | Hugging Face text-embeddings-inference (chosen) | Infinity |
|---|---|---|
| Release images | Versioned CPU tags on ghcr (`cpu-1.9.4`), multi-arch index, digest-pinnable | Versioned tags exist, but releases have been irregular |
| OpenAI API | `/v1/embeddings` built in, plus `/embed`, `/health`, `/info` | `/embeddings` (OpenAI shaped) |
| Local model dir | `MODEL_ID=/model` loads a mounted directory, no network | Supported |
| Private HF repo | `HF_TOKEN` | `HF_TOKEN` |
| CPU backend | Rust, ONNX/candle with MKL, small image | Python, PyTorch or ONNX, larger image |
| Tuning knobs | `MAX_BATCH_TOKENS`, `MAX_CLIENT_BATCH_SIZE`, `AUTO_TRUNCATE`, `POOLING`, `API_KEY` | Similar |

TEI supports the BERT, XLM-RoBERTa (BGE-M3 and its fine-tunes), NomicBERT,
JinaBERT, Qwen and Mistral embedding families. One model per service; deploy
a second copy of the role under another vault if two models are needed.

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skembed/deploy_skembed-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

What the play does:

1. Validates the image pin (`@sha256:` required), the model source (exactly
   one of `MODEL_PATH` or `MODEL_ID`) and the service knobs. A model or cache
   path outside `/var/data` (local disk) needs a `node.hostname == ...`
   placement constraint, or the play stops.
2. Renders `skembed.env` (0600: `HF_TOKEN`, `API_KEY`), the compose file and
   the deploy script into `/var/data/config/skembed-<env>/`.
3. Creates the `skembed-<env>` overlay and deploys stack `skembed-<env>`,
   service `skembed`. A redeploy that changed the env file restarts the
   service (an `env_file` change alone does not change the service spec).

## Configuration (vault)

```yaml
skembed:
  MODEL_PATH: ""             # directory with config.json, tokenizer and weights, mounted read-only at /model
  MODEL_ID: ""               # or a Hugging Face repo id (owner/name), downloaded into CACHE_PATH on first start
  MODEL_REVISION: ""         # optional HF revision (tag, branch or commit) with MODEL_ID
  MODEL_NAME: ""             # the name answered in the OpenAI `model` field (default: the model id)
  HF_TOKEN: ""               # token for a private HF repo (vault)
  API_KEY: ""                # optional: callers must send Authorization: Bearer <API_KEY>
  IMAGE: ""                  # default below; must carry @sha256:
  CACHE_PATH: ""             # default /var/data/skembed-<env>/cache (HF cache, bound at /data)
  PLACEMENT_CONSTRAINTS: []  # default ["node.role == worker"]
  REPLICAS: 1
  CPUS: "4.0"                # limit
  MEMORY_LIMIT: 6G
  MEMORY_RESERVATION: 2G
  MAX_BATCH_TOKENS: 16384
  MAX_CLIENT_BATCH_SIZE: 64
  POOLING: ""                # cls, mean, splade, last-token; empty = the model's own pooling config
  PUBLISHED_PORT: ""         # e.g. 18089: publish 8080 on the swarm ingress for off-estate callers
  HEALTH_START_PERIOD: 600s  # first start downloads or loads the model
  networks: []               # override the overlay network (subnet registry)
```

| Key | Default | Notes |
|---|---|---|
| `MODEL_PATH` / `MODEL_ID` | none | Exactly one. `MODEL_PATH` keeps the nodes offline (copy the model directory to shared storage once). `MODEL_ID` needs internet access from the node on first start. |
| `HF_TOKEN` | none | Secret. Only for a private repo with `MODEL_ID`. In the 0600 env file, and visible in `docker service inspect` like every `env_file` value: never print a service spec's env. |
| `API_KEY` | none | Secret. When set, TEI refuses calls without the bearer token. Give the same value to skcorpus (`EMBED_API_KEY`) and skai (`EMBED_API_KEY`). |
| `MODEL_NAME` | model id | Callers send it as `model`. Set it to the name the consumers are configured with (`skcorpus` corpus `embed_model`, `skai.EMBED_MODEL`). |
| `IMAGE` | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.4@sha256:2538ea1c...` | The CPU image, index digest. A GPU host can use a `cuda-*` or `89-*` tag of the same release instead. |
| `CPUS` / `MEMORY_*` | 4 CPUs, 6G limit, 2G reserved | A BGE-M3 sized model (568M parameters) uses about 2.5 GB resident. Sum what else is pinned to the node before raising limits. |
| `PUBLISHED_PORT` | none | Publishes on every swarm node. Set `API_KEY` with it, and firewall the port to the callers. |

## Networks

| Network | Subnet |
|---|---|
| `skembed-prod` / `-staging` / `-dev` | `172.16.126.0/24` / `172.16.127.0/24` / `172.16.128.0/24` |

## Instance actions

- Choose the model and set `MODEL_PATH` (preferred) or `MODEL_ID` (+ `HF_TOKEN`).
- Set `MODEL_NAME` to the name consumers use, and an `API_KEY` if anything off the overlay can reach it.
- Deploy skembed before skcorpus and skai (they attach to its network).

## Verification

```bash
docker service ls --filter name=skembed
# from a container on the skembed-<env> network:
curl -s http://skembed-<env>_skembed:8080/health
curl -s http://skembed-<env>_skembed:8080/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"<MODEL_NAME>","input":"hello"}' | head -c 200
```

Tests: `v1/tests/test_skembed_render.py` (the deploy through real Ansible:
digest pin, model source rules, placement guard, env file mode).
