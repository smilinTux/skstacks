# Estate SSO plan: one identity provider, the right pattern per service

Plan only. Nothing here is implemented except Nextcloud (see `sso-nextcloud-oidc.md`) and Windmill
(`skflow` role, PR 205). Placeholders: `*.example.com`. The inventory below comes from a live
Swarm estate with one Authentik (`sksso`) and one Traefik edge (`skfenceha`).

## Principles

1. Native OIDC (or SAML) wherever the app supports it. The app validates the token itself, so APIs,
   clients, websockets and webhooks keep working with no path carve-outs.
2. Forward auth only for apps with no usable login of their own, and then only as an allow-list of
   protected paths with an explicit, tested bypass list. Never as a blanket router middleware on an
   app that has an API, a websocket, a webhook or an agent protocol.
3. Authentik is the policy point: access groups per application, bound at the application.
4. Every app keeps a local break-glass admin that does not depend on the IdP.
5. Everything is code in SKStacks: blueprint per application, secrets by environment variable, a
   render test per pattern. Nothing is configured by hand twice.

## Inventory and recommended pattern

Routers and middlewares were read from the Swarm service labels. "FA" = Authentik forward auth.

| Service | Edge routing today | Auth today | Recommended pattern | Notes |
|---|---|---|---|---|
| sksso (Authentik) | host rule, no SSO middleware | itself | n/a, the IdP | recovery key + `akadmin` break-glass in vault |
| skhub Nextcloud | `skhub` (headers, dav redirect) | local + new `user_oidc` | native OIDC (done) | NEVER FA; see nextcloud doc |
| skhub notify_push, talk-hpb, whiteboard, collabora, imaginary | path routers on the Nextcloud host, `collabora` own host | token / shared secret | none (leave untouched) | called by Nextcloud and clients; FA breaks them |
| skflow (Windmill) | UI router has FA, `/api/` router bypasses, `/outpost` routed | native OIDC (works) AND FA | native OIDC only; drop FA | double login; FA also gates non-`/api/` paths such as websocket/LSP endpoints; webhooks live under `/api/` so they pass |
| skfwe worker, nextcloud-mcp | no public router | bearer token / app password | none; keep internal | reached over the overlay by Windmill |
| skmesh (NetBird) | dashboard + `/api`, `/management` (gRPC), `/signalexchange`, `/relay` | native OIDC (public client) | native OIDC (done) | never FA: gRPC, relay, signal |
| skrac (MeshCentral) | host rule + agent-path router (`/agent.ashx`, `/meshrelay.ashx`, `/meshagents`, ...) | native OIDC provider exists | native OIDC | agent paths are machine traffic and must stay open |
| skai (Open WebUI style) | external | native OIDC | native OIDC (done) | map `groups` claim to admin/user |
| Proxmox | file route to LAN host | native OIDC realm | native OIDC (done) | FA intercepts the callback and breaks login |
| Pi-hole | file route, FA | Pi-hole password | FA on `/admin/` UI only | no native OIDC; bypass nothing else because DNS is not HTTP, but exclude API consumers (exporters) with `/api/` allow by IP or token |
| skvector (Qdrant) | UI/REST router + gRPC router (`/qdrant.`) | API key | FA on `/dashboard` only | bypass `/collections`, `/points`, `/qdrant.`, `/healthz`, `/readyz`, `/livez`, `/metrics` (token) |
| skpayment | webhook router (`/webhook/stripe`, no auth) + UI router | app login | native OIDC if the app supports it, else FA on UI | webhook must stay open and signature-verified |
| skgit (Forgejo), Grafana, Open WebUI, Portainer, Gitea-likes | per host | local | native OIDC | all support generic OIDC and group/role claims |
| Uptime Kuma, Traefik dashboard, Syncthing GUI, similar admin-only tools | host rule | basic auth / none | FA, whole host except health/status pages | status page route bypassed explicitly |
| skreg registry, skmem-pg, skgraph, crowdsec, cloudflared, hbbs/hbbr | no HTTP edge | n/a | none | not user-facing |

## Forward-auth recipe (only when needed)

Define the middleware once in the edge's dynamic config:

```yaml
authentik:
  forwardAuth:
    address: "http://sksso-<env>_server:9000/outpost.goauthentik.io/auth/traefik"
    trustForwardHeader: true
    authResponseHeaders: [X-authentik-username, X-authentik-groups, X-authentik-email, X-authentik-name, X-authentik-uid]
```

Per application, three routers, in priority order:

1. `/outpost.goauthentik.io/` on the app host, priority highest, routed to the Authentik service
   (the login round trip needs it).
2. Bypass router for the exempt prefixes (APIs, webhooks, websocket, health, public shares), same
   host, higher priority than the app router, without the SSO middleware.
3. App router with the SSO middleware.

The `skflow` role already encodes this as `SSO_MIDDLEWARE`, `SSO_EXEMPT_PREFIXES` and
`SSO_OUTPOST_SERVICE`. Generalise that into a shared Traefik-label helper used by every role that
supports forward auth, and make an empty exempt list a refusal for any role that has an API.

Bypass list by kind of path (default deny, add only what the app needs): `/api/`, `/health*`,
`/status*`, `/metrics` (with its own token), `/webhook*`, `/ws*` and any websocket upgrade path,
`/.well-known/`, agent or client protocol paths, public share or invite links, ACME challenges.

## Groups and roles

- One access group per application: `<app>-users`, optional `<app>-admins`. The application binding
  lists those groups (default deny).
- A small set of global tiers (`admin`, `editor`, `readonly`) for portal tiles and for apps that map
  a single role from a `groups` claim.
- Claim contract: `groups` (list of group names), `email`, `name`, and for apps whose local
  usernames differ from the IdP, a per-app uid claim such as `nextcloud_uid` fed from a user
  attribute. Claims are emitted by scope mappings that fail closed (no attribute, no claim).
- Role mapping by claim where the app supports it (Proxmox groups, Windmill operators, Forgejo and
  Open WebUI admin group); otherwise one role per app and the group gate only.
- Onboarding is one script: create the Authentik user, add to the app groups, set the per-app uid
  attributes, check the local account exists. Offboarding reverses it.

## Break-glass and recovery

- Authentik: keep a non-SSO administrator (`akadmin` or a dedicated account) whose password is in the
  vault, a stored recovery-key procedure (`ak create_recovery_key`), and a tested restore of the
  Authentik database dump.
- Each app: one local admin with a vault password, tested after every SSO change and quarterly.
- Never hide a password form (`hide_login_form`, auto-redirect) until two people have signed in via
  SSO and the break-glass login has been tested the same day. Keep a `?direct=1`-style bypass where
  the app has one.
- Roll out in stages: add SSO next to passwords, verify, then (optionally) make it the default.

## Secrets

- Client secrets: generated once, written to the vault under a per-app key (for example
  `sso/<app>/client_secret`), injected into Authentik by blueprint `!Env` and into the app by a swarm
  secret or env file (0600). Never on argv, never in compose files, never in the repo.
- Rotation: change in the vault, redeploy the blueprint env and the app config in one play, verify the
  login-page redirect (see the Nextcloud verification list), then drop the old value.
- Service accounts keep app passwords or API tokens; they are not SSO identities. Record them in the
  vault with owner and expiry.

## Making it code in SKStacks

1. `sksso` role: one blueprint template per application under `blueprints/`, selected by a
   `sksso_apps` list in instance vars (client id, redirect URIs, groups, scope mappings, signing
   key). The Windmill blueprint from PR 205 is the first instance of the pattern.
2. App roles: an `sso:` block in each role (enable flag, issuer, client id, secret source) and a
   post-deploy task that applies it idempotently (`occ user_oidc:provider` for skhub; the `oauths`
   setting provisioner for skflow).
3. Tests, one per pattern, using the existing real-Ansible render harness: no forward-auth
   middleware on routers of apps declared `sso: oidc`; every forward-auth router has an outpost
   route and a non-empty bypass list; no secret values in rendered files.
4. Portal tile sync scripts must treat provider-backed apps as provider-backed. A tile of kind
   "link" that sets `provider: null` detaches an existing provider and removes group bindings it does
   not list. Declare the real kind and the groups there too.
5. Document the claim contract (names, per-app uid attributes) next to the blueprints so a new role
   copies the pattern instead of inventing one.

## Top recommendations, in order

1. Native OIDC wherever supported; forward auth only for apps with no auth, always with a tested
   bypass list. Add the render test that enforces both.
2. Remove the redundant forward auth from the Windmill UI router and rely on its native OIDC.
3. Make blueprints plus one reconciler the only way Authentik objects change; fix the tile sync so it
   cannot detach providers.
4. Standardise the group and claim model (`<app>-users`, `groups`, per-app uid attributes), then onboard
   the remaining Nextcloud accounts and only afterwards consider auto-redirect.
5. Break-glass and vault discipline: tested local admins, recovery key, per-app secret keys, rotation
   through one play.
