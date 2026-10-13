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
   worker`, networks `skpayment-<env>` and `cloud-public-<env>`, two Traefik
   routers on `websecure` with the `main` cert resolver (the site, and the
   Stripe webhook path; see "CrowdSec"). A redeploy that
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
  MIDDLEWARES: ""                 # site router; default default@file (see "CrowdSec" below)
  WEBHOOK_PATH: ""                # default /webhook/stripe; "none" = one router for everything
  WEBHOOK_MIDDLEWARES: ""         # webhook router; default default-no-crowdsec@file
  WEBHOOK_PRIORITY: ""            # webhook router priority; default 1000
  WEBHOOK_IP_ALLOWLIST: false     # true: only WEBHOOK_SOURCE_RANGES reach the webhook path
  WEBHOOK_SOURCE_RANGES: []       # default: Stripe's published webhook IPs (below)
  WEBHOOK_IP_STRATEGY_DEPTH: ""   # Traefik ipStrategy.depth for the allowlist; default unset
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

A payment page should sit behind CrowdSec, but Stripe's webhook callers must
never be banned (a banned Stripe IP silently stops subscription updates). So
the role renders two routers on the same host and service:

| Router | Rule | Middlewares (default) | Priority |
|---|---|---|---|
| `skpayment[-env]` (site) | `Host(<HOSTNAME>)` | `MIDDLEWARES`: `default@file` | Traefik default (the rule's length) |
| `skpayment[-env]-webhook` | `Host(<HOSTNAME>) && Path(<WEBHOOK_PATH>)` | `WEBHOOK_MIDDLEWARES`: `default-no-crowdsec@file` | `WEBHOOK_PRIORITY`: 1000 |

`default@file` includes the CrowdSec bouncer when the instance's skfenceha
has `CROWDSEC_ENABLED` (and sksec runs); otherwise it is the same chain
without it. Set `MIDDLEWARES` to the instance's own CrowdSec chain if it
uses a different name. The webhook router matches only the exact path
`WEBHOOK_PATH` (the app's handler is `POST /webhook/stripe`), so checkout,
portal, status and every other path stay on the CrowdSec router. Both
routers share TLS settings and the backend.

Why the exemption is safe: the app verifies every webhook's
`Stripe-Signature` (HMAC-SHA256 with `STRIPE_WEBHOOK_SECRET`) and answers
400 to a missing, wrong or (from v1.1.0) replayed signature, and 503 to every
webhook when no webhook secret is set. The deploy already refuses the one
app release that accepted unsigned webhooks without a secret (see "Stripe not
configured"). The webhook router keeps the rate limit, security headers and
error pages of `default-no-crowdsec@file`.

`WEBHOOK_PATH: none` restores the single-router layout (everything on
`MIDDLEWARES`); before v2.26.7 the only router used
`default-no-crowdsec@file`, so an instance that wants the old behaviour sets
`WEBHOOK_PATH: none` and `MIDDLEWARES: default-no-crowdsec@file`.

#### Optional: Stripe IP allowlist on the webhook path

`WEBHOOK_IP_ALLOWLIST: true` adds an `ipAllowList` middleware
(`skpayment[-env]-stripe-ips`, defined in the service labels) in front of
the webhook chain, so only Stripe's published webhook source IPs reach the
path. Off by default: the signature check already makes forged webhooks
useless, and the list changes over time. The default list is Stripe's
`https://stripe.com/files/ips/ips_webhooks.json` as of 2026-10-13 (15
addresses); check it before enabling and override with
`WEBHOOK_SOURCE_RANGES` (a list of IPs/CIDRs) when Stripe updates it.

The allowlist checks the address Traefik considers the client. With
Traefik receiving connections directly (host-mode ports, or PROXY protocol
from the HA ingress) that is the real caller and no depth is needed. Behind
another proxy (a Cloudflare tunnel or proxied DNS record) the connection
comes from the proxy: set `WEBHOOK_IP_STRATEGY_DEPTH` to the position of the
real client in `X-Forwarded-For` counted from the right (usually `1` behind
one proxy), and only when Traefik's entrypoint trusts that proxy's forwarded
headers; otherwise every webhook is refused with 403. Behind a Cloudflare
tunnel (cloudflared) the real caller may only arrive in `Cf-Connecting-Ip`,
which `ipAllowList` cannot read, and Traefik sees the tunnel's own address:
leave the allowlist off there. The same applies to CrowdSec itself: the
bouncer can only ban the address Traefik passes it. Verify with a Stripe
test-mode event after enabling.

## Migrating a hand-deployed gateway

The hand-deployed stack (`skpayment`, code at `/var/data/skpayment/skpayment.py`,
database in a named volume bound to `/var/data/skpayment`, config at
`/var/data/config/skpayment/apps.json`) maps onto this role as:

- `HOSTNAME: pay.<domain>` (the hand-deployed router used
  `default-no-crowdsec@file` for everything; the role defaults put the site
  behind `default@file` and exempt only the webhook path, see "CrowdSec"),
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
