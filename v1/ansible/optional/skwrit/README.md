# skwrit

The worker of the skwrit matter engine: it turns taught processes into
verified paperwork (drafting with quality gates, an independent reviewer,
release, publishing into Nextcloud with a re-download check). Opt-in:
nothing renders or changes for an instance that never runs
`deploy_skwrit-<env>.yml`.

The engine and its packs live in their own (private) repository and ship as
a container image built per site. This role deploys that image.

## Design

- One `worker` service (name set by `WORKER_SERVICE`) on port 8080, one
  replica, `stop-first` updates with `failure_action: rollback`, a
  `/health` healthcheck. The workflow engine (skflow) calls it over the
  `WORKFLOW_NETWORK` overlay as `http://<STACK_NAME>_<WORKER_SERVICE>:8080`.
- The image has no default: `SKWRIT_IMAGE` is built per site and the play
  refuses one without `@sha256:`.
- Settings reach the worker as `SKWRIT_*` environment variables (the engine
  still reads the legacy `FWE_*` names as a fallback).
- Secrets are **external Swarm secrets referenced by name**. This play never
  reads, writes or templates a secret value; `deploy` refuses to start when
  one of them or the engine overlay is missing.
- The state volume (`/state`) is external and already holds the worker's
  database and private render assets.
- Optional `nextcloud-mcp` service (`NEXTCLOUD_MCP_ENABLED`), digest-pinned.

### Prerequisites made by hand

```bash
printf '%s' "$VALUE" | docker secret create skwrit_nextcloud_v1 -   # likewise skwrit_worker_v1, skwrit_vector_v1
docker volume create <STATE_VOLUME>        # on the node the worker is placed on
```

The `WORKFLOW_NETWORK` overlay is the skflow role's tenant network.

### Keeping an existing service name

`STACK_NAME` and `WORKER_SERVICE` set the Swarm DNS name. An instance that
already runs the worker under another name sets both to keep it (flows keep
calling the same URL), and renames later in one planned change.

## Deployment

```bash
ansible-playbook -i <inventory> \
  framework/v1/ansible/optional/skwrit/deploy_skwrit-<env>.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file <vault password file>
```

## Configuration (vault)

`skwrit:` in `optional/group_vars/<env>/skwrit-<env>-<domain dashed>-<cluster>_vault.yml`.

```yaml
skwrit:
  # REQUIRED (no default)
  SKWRIT_IMAGE: ""                  # worker image pinned by digest (@sha256:)
  NEXTCLOUD_URL: ""                 # http(s) base URL of Nextcloud

  # worker settings, each written as SKWRIT_<NAME> only when set
  MODEL: ""                         # composer model
  MODEL_URL: ""                     # chat completions URL for the composer
  REVIEW_MODEL: ""                  # independent reviewer model (must differ from MODEL)
  REVIEW_URL: ""                    # reviewer URL, default MODEL_URL
  VECTOR_URL: ""                    # vector store base URL
  EMBED_URL: ""                     # embeddings URL

  # naming and wiring
  STACK_NAME: skwrit-<env>          # Swarm stack and internal overlay name
  WORKER_SERVICE: worker            # service name inside the stack
  WORKFLOW_NETWORK: skflow-<env>    # existing overlay shared with skflow
  INTERNAL_SUBNET: ""               # stack-local overlay subnet; default per env (prod 172.16.249.0/24)
  PLACEMENT_CONSTRAINTS: ["node.role == worker"]
  EXTRA_HOSTS: []                   # entries "host:address" for names the overlay DNS cannot resolve
  MEMORY_LIMIT: 2G

  # external Swarm secrets (names only) and volumes
  NEXTCLOUD_SWARM_REF: skwrit_nextcloud_v1     # name of the Swarm secret with the Nextcloud account password (target nextcloud_password)
  WORKER_SWARM_REF: skwrit_worker_v1           # name of the Swarm secret with the worker bearer value (target worker_token)
  VECTOR_SWARM_REF: skwrit_vector_v1           # name of the Swarm secret with the vector store access value (target vector_api_key)
  STATE_VOLUME: <STACK_NAME>_state

  # optional Nextcloud MCP service
  NEXTCLOUD_MCP_ENABLED: false
  NEXTCLOUD_MCP_IMAGE: ""           # default nextcloud-mcp-server 0.199.4 by digest
  NEXTCLOUD_MCP_USERNAME: svc-skwrit
  MCP_DATA_VOLUME: <STACK_NAME>_mcp_data
  MCP_MEMORY_LIMIT: 2G

  # logging and health
  LOG_MAX_SIZE: 10m
  LOG_MAX_FILES: 3
  HEALTH_CHECK_INTERVAL: 30s
  HEALTH_CHECK_TIMEOUT: 5s
  HEALTH_CHECK_RETRIES: 3
```

## Upgrades

Build and push the new image, then change `SKWRIT_IMAGE` (tag and digest
together) and run the playbook. A failed update rolls back by itself. For a
manual rollback, `docker service update --rollback <stack>_<service>`.

## Health check

`/health` on the worker returns the corpus, gate and asset status; an
`assets.ok: false` means the private render assets are missing from the
state volume.
