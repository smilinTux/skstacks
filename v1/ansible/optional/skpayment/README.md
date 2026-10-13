# skpayment

SKPayment is a multi-app Stripe subscription gateway (FastAPI on uvicorn):
apps call `POST /checkout/<app>` and `/portal/<app>`, Stripe calls
`POST /webhook/stripe`, and on subscription changes it sets the user's
budget and rate limits in that app's LiteLLM. One replica, a SQLite customer
database, `GET /health`.

The app lives in its own repository (`smilinTux/skpayments`, the single file
`skpayment.py`). This role does not vendor it: the deploy fetches it at a
pinned ref and verifies its sha256, then runs it read-only inside a
digest-pinned Python image that already ships fastapi, uvicorn and httpx.

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skpayment/deploy_skpayment-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

What the play does:

1. On the **controller** (the machine running Ansible): `git clone` the app
   repo, check out `APP_REF` (a tag or a commit sha), and stop unless
   `skpayment.py` there hashes to `APP_SHA256`. A moved tag, a force-push or
   a typo stops the deploy; nothing is copied. The controller needs `git` and
   read access to the repo (a private repo uses the operator's own git
   credentials; the swarm nodes need none and need no internet access to
   GitHub). The checkout is removed afterwards.
2. Copies the verified file to `/var/data/skpayment-<env>/app/skpayment.py`
   (bind-mounted read-only at `/skpayment.py`).
3. Renders `skpayment.env` (Stripe keys, `BASE_URL`, 0600) and `apps.json`
   (per-app config including LiteLLM master keys, 0600, bind-mounted
   read-only at `/config/apps.json`) into `/var/data/config/skpayment-<env>/`.
4. Deploys stack `skpayment-<env>`, service `skpayment`: `node.role ==
   worker`, networks `skpayment-<env>` and `cloud-public-<env>`, Traefik
   router on `websecure` with the `main` cert resolver. A redeploy that
   changed the code, `apps.json` or the env file restarts the service (bind
   mounts do not change the service spec by themselves).

Upgrading the app = set the new `APP_REF` and its `APP_SHA256`
(`git show <ref>:skpayment.py | sha256sum`) and redeploy.

## Configuration (vault)

```yaml
skpayment:
  CLUSTERNAME: skstack01          # default: inventory cluster_name
  DOMAIN: example.com             # default: inventory domain
  CLOUDFLARED: false              # true: default host is under DOMAIN, not CLUSTERNAME.DOMAIN
  HOSTNAME: ""                    # public host; default skpayment[-dev|-staging].<base domain>
  STRIPE_SECRET_KEY: "sk_live_..."      # both or neither; both empty = "Stripe not configured"
  STRIPE_WEBHOOK_SECRET: "whsec_..."    # both or neither
  APPS: {}                        # the apps.json content (see the app repo's apps-example.json)
  IMAGE: ""                       # default below; must carry @sha256:
  APP_REPO: ""                    # default https://github.com/smilinTux/skpayments.git
  APP_REF: ""                     # default d3138a22b8b24553ebf552fed7315c8d413cd399 (v1.1.0)
  APP_SHA256: ""                  # default 5a88f86bf6b22ba991568152cd3341d80afa67e003478787fb7eaa1186093d8e
  DATA_PATH: ""                   # default /var/data/skpayment-<env>/data (SQLite, bound at /data)
  DATA_NODE: ""                   # pin the service to this node (required with a local DATA_PATH)
  MIDDLEWARES: ""                 # default default-no-crowdsec@file (see "CrowdSec" below)
  TLS_OPTIONS: ""                 # default intermediate@file; "none" = no tls.options label (Traefik default options)
  networks: []                    # override the overlay networks (subnet registry)
```

| Key | Default | Notes |
|---|---|---|
| `IMAGE` | `ghcr.io/open-webui/open-webui:main@sha256:74093dad...` | Used only as a Python base with fastapi/uvicorn/httpx, the same image digest the hand-deployed gateway ran. Any digest-pinned image with Python 3.10+ and those three packages works; a small dedicated image would pull far less (about 1.7 GB compressed for this one). The deploy refuses an image without `@sha256:`. The image's own HEALTHCHECK probes :8080, which nothing serves here, so the service sets its own on `:8000/health`. |
| `APP_REF` / `APP_SHA256` | the app repo's `v1.1.0` commit and its file hash | Both are required in effect: the ref says where, the hash proves what. Before v2.26.6 the default was the first release (`17b0080`), which accepted unsigned webhooks when `STRIPE_WEBHOOK_SECRET` was empty. |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | none | Both set, or both empty ("Stripe not configured", below). One without the other is refused. |
| `DATA_PATH` / `DATA_NODE` | shared storage, floating | SQLite over NFS works for this write volume (a few rows per subscription event). For local disk set both; the play stops on a local `DATA_PATH` without `DATA_NODE`, same rule as skgraph/skvector. |
| `TLS_OPTIONS` | `intermediate@file` (sniStrict) | A file-provider TLS option. `none` omits the label, so the router gets Traefik's default TLS options (the built-in option named `default` cannot be referenced as `default@file`: Traefik rejects the router with "unknown TLS options"). With sniStrict, a host without a certificate fails the handshake (a lab without ACME). |
| `HOSTNAME` | `skpayment[-env].<base domain>` | `<base domain>` is `DOMAIN` with `CLOUDFLARED: true`, else `CLUSTERNAME.DOMAIN` (the same expressions the skhub routers use since v2.26.3). The gateway's `BASE_URL` is `https://<HOSTNAME>`. |

### Stripe not configured

With both Stripe secrets empty (or absent) the gateway still deploys and
starts: `/health` answers 200 with `"stripe_configured": false`, `/status`
works, `/checkout/<app>` and `/portal/<app>` answer 503 "Stripe not
configured", and `/webhook/stripe` answers 503 to EVERY request, signed or
not. Use it to stand the gateway up before a Stripe account exists, instead
of placeholder keys. The deploy refuses this mode when `APP_REF`/`APP_SHA256`
pin the first app release (`17b0080`), whose code ACCEPTED unsigned webhooks
without a webhook secret (anyone could have posted
`checkout.session.completed` and been granted a tier). From v1.1.0 the app
also refuses signed events older than 5 minutes (replays).

Secrets: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` and every
`APPS.<app>.litellm_master_key` are secrets. They are only written to the two
0600 files (and, because `env_file` values are copied into the swarm service
spec, they are visible to `docker service inspect`: never print a service
spec's env).

### CrowdSec

`MIDDLEWARES` defaults to `default-no-crowdsec@file`, matching the
hand-deployed gateway. Whether that was deliberate is unknown. A plausible
reason is that Stripe's webhook callers must never be banned; the risk of the
default is that the public checkout and webhook endpoints get no CrowdSec
protection when the instance runs sksec. To put it behind the framework's
normal chain set `MIDDLEWARES: default@file` (which includes CrowdSec when
skfenceha has `CROWDSEC_ENABLED`), and allowlist Stripe's webhook source IPs
in sksec first.

## Migrating a hand-deployed gateway

The hand-deployed stack (`skpayment`, code at `/var/data/skpayment/skpayment.py`,
database in a named volume bound to `/var/data/skpayment`, config at
`/var/data/config/skpayment/apps.json`) maps onto this role as:

- `HOSTNAME: pay.<domain>`, `MIDDLEWARES: default-no-crowdsec@file` (default),
  `APP_REF`/`APP_SHA256` (defaults: v1.1.0. The hand-deployed file is the
  first release, `17b0080`, sha256 `c957335b...`; check with `sha256sum`.
  The default upgrades it to v1.1.0, which rejects unverified webhooks);
- `APPS`: the current `apps.json` content, moved into the vault;
- the database: either `DATA_PATH: /var/data/skpayment` (keeps the file where
  it is; `skpayment.db` lives directly in it) or copy `skpayment.db` into
  `/var/data/skpayment-prod/data/` while the old service is scaled to 0.

Then remove the old stack (`docker stack rm skpayment`) and deploy this one:
the router name is the same (`skpayment` in prod), so two stacks must not run
at once. The stack name, network (`skpayment-prod`, subnet 172.16.235.0/24)
and service name (`skpayment-prod_skpayment`) differ from the hand-deployed
ones.

## Networks

| Network | Subnet |
|---|---|
| `skpayment-prod` / `-staging` / `-dev` | `172.16.235.0/24` / `172.16.236.0/24` / `172.16.237.0/24` |
| `cloud-public-<env>` | framework-shared Traefik network |

## Verification

```bash
docker service ls --filter name=skpayment
curl -s https://<HOSTNAME>/health      # {"status":"ok","service":"skpayment","apps":[...]}
```

Tests: `v1/tests/test_skpayment_render.py` (the deploy through real Ansible,
and the controller fetch run for real against a local git repo: tag and sha
refs, a sha256 mismatch and an unknown ref both fail closed). The skstack06
release harness deploys it and checks `/health` through skfenceha.
