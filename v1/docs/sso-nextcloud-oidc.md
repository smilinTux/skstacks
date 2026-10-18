# Nextcloud (skhub) single sign-on with Authentik: native OIDC, no forward auth

Status: proven on a live prod instance (Nextcloud 34.0.4, `user_oidc` 8.11.0, Authentik 2026.2.1).
Everything below uses placeholders: `cloud.example.com` is the Nextcloud host, `sso.example.com` is
Authentik. Site values live in the operator's private config repo.

## Decision: native OIDC, never forward auth in front of Nextcloud

Do not attach an Authentik forward-auth middleware (the `authentik@file` Traefik middleware) to any
router of the skhub stack. Nextcloud is not a browser-only app. Its clients authenticate with tokens
and app passwords, and a forward-auth redirect to an HTML login page answers every one of them with a
302 they cannot follow:

| Path or client | Why forward auth breaks it |
|---|---|
| `/push/*` (notify_push websocket) | no cookie jar, no redirect handling; push silently dies |
| `/remote.php/dav`, `/.well-known/caldav`, `/.well-known/carddav` | WebDAV/CalDAV/CardDAV clients, desktop and mobile sync, service accounts with app passwords |
| `/ocs/*`, `/index.php/apps/*/api` | OCS API consumers, bots, Talk bots, webhooks |
| `/standalone-signaling/*` (Talk HPB) | signaling websocket; the HPB also calls back into Nextcloud |
| `/whiteboard/*`, Collabora `/cool/ws`, WOPI | token-authenticated, called by other services |
| public share links `/s/<token>`, `/index.php/s/` | anonymous by design |

The skhub role therefore ships no SSO middleware on any router, and that must stay true. A render
test should assert it (see "Guard rails").

Nextcloud's own `user_oidc` app does the job at the application layer: it adds a "Login with SSO"
button, validates the ID token, and logs the person into a normal Nextcloud session. Password login,
app passwords, WebDAV and every client keep working because they never touch the OIDC path.

## Identity mapping: log into the EXISTING accounts, never create duplicates

Nextcloud usernames and IdP usernames rarely match one for one. Do not rely on `preferred_username`
or on `sub` (a hash, which would create a second account per person).

1. In Authentik give each person a user attribute `nextcloud_uid` holding the exact existing
   Nextcloud uid.
2. A scope mapping (scope name `nextcloud`) emits it as a claim and returns an empty object when the
   attribute is missing, so a person without a mapped uid gets no claim and is refused (fail closed):

   ```python
   uid = request.user.attributes.get("nextcloud_uid")
   if not uid:
       return {}
   return {"nextcloud_uid": uid}
   ```
3. `user_oidc` provider settings: `--mapping-uid=nextcloud_uid --unique-uid=0` (uid used verbatim,
   no provider-hash prefix), scope `openid email profile nextcloud`.
4. System config `user_oidc.auto_provision = false`. With it off, `user_oidc` never creates an
   account: it only logs in an account that already exists in any backend. This is the duplicate
   guard. (Default is on, where a missing account is created and an existing local account is adopted
   when `soft_auto_provision` is true.) Keep `soft_auto_provision = true`.
5. `allow_multiple_user_backends = 1` (app config) so the normal password form and app passwords
   stay available. Do not set `hide_login_form` until break-glass is verified (see below).

## Authentik objects

- OAuth2/OIDC provider: confidential, client id `skhub`, signing key = the self-signed certificate
  (RS256, JWKS published at `.../application/o/skhub/jwks/`), subject mode `user_id`, issuer mode
  per provider, strict redirect URI `https://cloud.example.com/apps/user_oidc/code`, scope mappings
  openid, email, profile plus the `nextcloud` mapping above. Authorization flow: implicit consent.
- Application: slug `skhub`, provider attached, launch URL the Nextcloud host.
- Group `skhub-users` bound to the application (policy binding, order 0). Only members can complete
  the Authentik side of the flow. Add a person to this group and set their `nextcloud_uid` attribute
  in one step; either alone is not enough.
- Client secret: generated once, stored in Authentik and Nextcloud only, plus the operator vault.

As a blueprint (reference, wire it like the Windmill blueprint in the sksso role; the secret comes
from an environment variable, never from the file):

```yaml
version: 1
metadata:
  name: skhub-nextcloud (managed by skstacks sksso)
  labels:
    blueprints.goauthentik.io/instantiate: "true"
entries:
  - model: authentik_core.group
    id: users-group
    identifiers: {name: skhub-users}
    attrs: {}
  - model: authentik_providers_oauth2.scopemapping
    id: nextcloud-claim
    identifiers: {name: "skhub: nextcloud_uid claim"}
    attrs:
      scope_name: nextcloud
      expression: |
        uid = request.user.attributes.get("nextcloud_uid")
        if not uid:
            return {}
        return {"nextcloud_uid": uid}
  - model: authentik_providers_oauth2.oauth2provider
    id: provider
    identifiers: {name: skhub-nextcloud}
    attrs:
      client_type: confidential
      client_id: skhub
      client_secret: !Env SKHUB_OIDC_CLIENT_SECRET
      authorization_flow: !Find [authentik_flows.flow, [slug, default-provider-authorization-implicit-consent]]
      invalidation_flow: !Find [authentik_flows.flow, [slug, default-provider-invalidation-flow]]
      signing_key: !Find [authentik_crypto.certificatekeypair, [name, authentik Self-signed Certificate]]
      sub_mode: user_id
      redirect_uris:
        - matching_mode: strict
          url: https://cloud.example.com/apps/user_oidc/code
      property_mappings:
        - !Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-openid]]
        - !Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-email]]
        - !Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-profile]]
        - !KeyOf nextcloud-claim
  - model: authentik_core.application
    id: application
    identifiers: {slug: skhub}
    attrs:
      name: SKHub (Nextcloud)
      provider: !KeyOf provider
      meta_launch_url: https://cloud.example.com
  - model: authentik_policies.policybinding
    identifiers:
      target: !KeyOf application
      group: !KeyOf users-group
      order: 0
    attrs: {enabled: true, negate: false, timeout: 30}
```

The per-user `nextcloud_uid` attribute is data, not config: set it from the operator's onboarding
script or the Authentik admin UI.

## Nextcloud commands

```bash
C=$(docker ps -q -f name=skhub-prod_nextcloud | head -1)
occ() { docker exec -i -u www-data "$C" php occ "$@"; }

occ app:install user_oidc            # 8.11.0 supports Nextcloud 29 to 35
# secret on stdin, never on argv or in shell history:
read -rs OIDCSEC; export OIDCSEC     # or: docker exec -i ... sh -c 'IFS= read -r OIDCSEC; export OIDCSEC; php occ ...'
occ user_oidc:provider "SSO" \
  --clientid=skhub --clientsecret-env=OIDCSEC \
  --discoveryuri=https://sso.example.com/application/o/skhub/.well-known/openid-configuration \
  --scope="openid email profile nextcloud" \
  --unique-uid=0 --mapping-uid=nextcloud_uid \
  --mapping-email=email --mapping-display-name=name \
  --check-bearer=0 --bearer-provisioning=0 --send-id-token-hint=1 --group-provisioning=0
occ config:system:set user_oidc auto_provision --value=false --type=boolean
occ config:system:set user_oidc soft_auto_provision --value=true --type=boolean
occ config:app:set user_oidc allow_multiple_user_backends --value=1
```

`--check-bearer=0` matters: with it on, `user_oidc` would validate WebDAV/OCS Bearer tokens against
the IdP, a different (and unneeded) integration. Service accounts keep using app passwords.

## Verification without a human

Interactive login needs the person's IdP password, so check everything around it:

1. `occ user_oidc:provider` lists the provider; the discovery URL is fetchable from the container.
2. The login page's initial state (`core-alternativeLogins`) lists "Login with SSO" pointing at
   `/apps/user_oidc/login/<id>`.
3. `GET /apps/user_oidc/login/<id>` returns 303 to the IdP authorize URL with the right
   `client_id`, `redirect_uri`, `scope`, PKCE challenge and an `essential` `nextcloud_uid` claim
   request.
4. Following that URL returns 302 to the IdP login flow (not an error page). A wrong redirect URI or
   unknown client id shows an error here instead.
5. Evaluate the scope mapping in `ak shell` for each person: it returns the uid for mapped users and
   `{}` for unmapped ones.
6. Regression: WebDAV PROPFIND and GET as a service account, CalDAV and CardDAV PROPFIND, OCS user
   and capabilities, Talk rooms, `occ notify_push:self-test`, signaling `/api/v1/welcome`, and any
   workflow engine that talks to Nextcloud.
7. A person tries the button once. Expected: land in their existing account (same files, same
   groups), no new user in `occ user:list`.

## Rollback (each step independent, all reversible)

```bash
occ user_oidc:provider:delete "SSO"        # removes the button and the provider
occ app:disable user_oidc                  # removes the app's login path entirely
occ config:system:delete user_oidc         # drops auto_provision overrides
```

In Authentik detach the provider from the `skhub` application (the application keeps working as a
plain link tile) and delete the provider and group. Passwords and app passwords were never touched,
so nothing needs restoring on the Nextcloud side.

## Break-glass and moving to "SSO only"

Leave the password form on until two people have signed in through SSO and the local admin password
is in the vault. To make SSO the default later: `occ config:app:set user_oidc allow_multiple_user_backends --value=0`
auto-redirects the login page to the IdP; `https://cloud.example.com/login?direct=1` still shows the
password form. Keep one local Nextcloud admin with a vault-stored password for the day the IdP is
down.

## Guard rails to add in this repo

- Render test: no router label of the skhub stack references `authentik` or any forward-auth
  middleware.
- Operators with a tile-sync script for Authentik applications must keep the `skhub` entry as a
  provider-backed tile, or the next run detaches the provider and removes the group binding.
- A post-deploy task `tasks/oidc_config.yml` (skhub) running the occ block above only when
  `SKHUB_OIDC_ENABLED` is true, with the secret from a swarm secret via `--clientsecret-file`.
