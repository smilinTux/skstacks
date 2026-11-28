# skseek (Perplexica)

An AI search UI that answers from live search results. It reads SearXNG through **skpeek**, which must already be
deployed in the same environment; skseek joins skpeek's overlay and reaches it as `skpeek-<env>_skpeek:8080`.
The router carries the sksso forward-auth middleware by default (`SSO_MIDDLEWARE: none` opts out). See
`docs/v1-service-catalog.md` for the deploy order, sign-in and the API router.

## Vault keys

All keys live under `skseek:`. Nothing is required; every model provider is off until its key is set.

| Key | Default | Meaning |
| --- | --- | --- |
| `IMAGE` | `smilintux/skseek-runtime:1.12.1-20261128@sha256:<digest>` | The runtime image, built from [`image/`](./image/) (see "Image"). Always wins. The older play-level variable `skseek_image` is still honoured when `IMAGE` is unset. |
| `TRAEFIK_MIDDLEWARES`, `API_MIDDLEWARES`, `SSO_MIDDLEWARE` | see the catalog | Router chains. |
| `PORT`, `HOSTNAME` | `3000`, `0.0.0.0` | Listen port and address inside the container. |
| `MEMORY_LIMIT`, `MEMORY_RESERVATION` | `2G`, `512M` | Swarm memory limit and reservation. |
| `CONFIG_TOML_PATH` | `/var/data/config/skseek-<env>/config.toml` | Host path of the rendered `config.toml`, mounted read-only. |
| `SEARXNG_API_URL`, `SIMILARITY_MEASURE`, `KEEP_ALIVE` | skpeek of the same env, `cosine`, `5m` | Search backend and model settings. |
| `OPENAI_API_KEY`, `GROQ_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY` | empty | Secrets. A provider with an empty key stays unavailable. |
| `CUSTOM_OPENAI_API_KEY`, `CUSTOM_OPENAI_API_URL`, `CUSTOM_OPENAI_MODEL_NAME`, `OLLAMA_API_URL`, `LM_STUDIO_API_URL` | empty | Custom and local providers. |
| `CLUSTERNAME`, `DOMAIN`, `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_WS_URL` | derived | Public host name and the URLs the browser app calls. |
| `AUTHENTIK_TILE*` | see the catalog | Portal tile. |

## Image

The default is `smilintux/skseek-runtime:1.12.1-20261128@sha256:<digest>` (the exact digest is in
`src/config/skseek/skseek.yml.j2`), built from [`image/`](./image/). It replaces the upstream
`itzcrazykns1337/perplexica:latest` image the role pinned before, which upstream has not rebuilt since January
and which carried fixable CRITICAL and HIGH findings (Trivy 0.76.2, `--severity CRITICAL,HIGH --ignore-unfixed`:
see the CHANGELOG entry for the before and after counts).

Layout, port (3000), data directory (`/home/perplexica/data`) and `config.toml` mount are unchanged, so no other
vault key changes.

**Why a layered rebuild.** The upstream image bundles a SearXNG checkout and Python venv that upstream builds from
the head of the SearXNG repository at build time, so a from-scratch build could not reproduce what runs today.
The application layer, where the fixable Node findings are, is rebuilt from the upstream source commit pinned in
the Dockerfile (Perplexica v1.12.1) with `next` bumped to a patched release. The runtime stage starts from the
upstream image digest, runs `apt-get -y upgrade`, purges the compilers and `-dev` packages (which also removes
the `linux-libc-dev` kernel header findings), upgrades the SearXNG venv packages, takes `node` from the
current Node 24 image (npm, npx and corepack are removed: only `node server.js` runs) and replaces `/home/perplexica` with the rebuilt app.

Two deliberate differences from upstream's image: the ONNX Runtime CUDA execution provider is not installed (its
download is gone upstream, and the nodes have no GPU libraries, so it was never usable), and `certifi` and `lxml`
in the SearXNG venv are newer than the versions SearXNG pins (`pip check` reports exactly those two).

**Rebuild** (after an OS or Node security update, or to take a newer `next`):

```bash
cd v1/ansible/optional/skseek
docker build -t smilintux/skseek-runtime:1.12.1-$(date +%Y%m%d) image/
# scan: trivy image --severity CRITICAL,HIGH --ignore-unfixed <image>   (pinned: aquasec/trivy:0.76.2)
# smoke test: run it with the role's env (SEARXNG_API_URL, a config.toml, a data dir) and curl port 3000
docker push smilintux/skseek-runtime:1.12.1-$(date +%Y%m%d)
```

Pin the pushed manifest digest (`docker push` prints it) as the default in `src/config/skseek/skseek.yml.j2`. An
instance that wants its own build sets `skseek.IMAGE` instead. Refresh the digest-pinned `node` base in
`image/Dockerfile` in the same commit.
