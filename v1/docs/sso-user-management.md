# Operator runbook: people, passwords and access with skhub SSO enabled

For operators and agents. Placeholders only: `cloud.example.com` is the Nextcloud (skhub) host,
`sso.example.com` is Authentik (sksso), `flow.example.com` is Windmill (skflow). Site values live in
the operator's private config repo.

Read first, in this order: [sso-nextcloud-oidc.md](sso-nextcloud-oidc.md) (how the login works and why),
[sso-provisioning.md](sso-provisioning.md) (the model: claims, groups, just-in-time accounts),
[sso-estate-plan.md](sso-estate-plan.md) (pattern per service). This page is the do-this-now version.
Role docs: [sksso](../ansible/optional/sksso/README.md), [skhub](../ansible/optional/skhub/README.md),
[skflow](../ansible/optional/skflow/README.md).

## How it works in five lines

1. Authentik is the only place a person's password, MFA and active/inactive state live.
2. Nextcloud logs people in through its `user_oidc` app. The ID token carries a `nextcloud_uid` claim
   (the Nextcloud account id) and a `nextcloud_groups` claim.
3. Access to Nextcloud = membership of the Authentik group `skhub-users`. Access to Windmill =
   membership of `skflow-operators`.
4. The first login creates the Nextcloud account, or adopts an existing local account with the same
   uid (no duplicate). That is just-in-time provisioning: there is no separate "create the user in
   Nextcloud" step.
5. Clients that cannot do a browser login (desktop sync, phones, WebDAV, calendars, bots) use Nextcloud
   app passwords, not SSO.

## Claims and groups

| Thing | Rule |
|---|---|
| `nextcloud_uid` | Authentik user attribute of that name if set, otherwise the Authentik username. The fallback value is written back to the attribute on the first login, so a later rename of the Authentik user never creates a second Nextcloud account. |
| Username mismatch | When a person's existing Nextcloud uid differs from the Authentik username they want, set the attribute `nextcloud_uid` to the existing Nextcloud uid BEFORE their first SSO login. The login then lands in the old account with its files and shares. Example: Authentik `pat.example` with `nextcloud_uid: pat` maps to the Nextcloud account `pat`. |
| `nextcloud_groups` | Names of the person's Authentik groups that start with `nc-`, prefix removed. Authentik `nc-ops-team` becomes Nextcloud `ops-team`. |
| Which groups SSO may manage | Only Nextcloud groups matching the provider's `group-whitelist-regex` (for example `^(ops-team|hh-.+)$`). SSO adds the person to claimed groups and removes them from whitelisted groups that are missing from the claim. Every other Nextcloud group (admin, local project groups) is never touched. If the claim is absent nothing changes. |
| Household groups | Convention `hh-<household>` in Nextcloud, `nc-hh-<household>` in Authentik. |

To let SSO manage a new kind of group, add its name pattern to the whitelist regex with
`occ user_oidc:provider "<identifier>" --group-whitelist-regex=...` (restate the whole regex).

## Add a person

1. Authentik, Directory, Users, Create: username (this becomes the Nextcloud uid unless you set the
   attribute), display name, email.
2. If they already have a Nextcloud account under a different uid, set attribute `nextcloud_uid` to that
   uid now.
3. Add them to `skhub-users` (Nextcloud), to `skflow-operators` if they use Windmill (Community Edition
   allows at most 10 SSO users in total), and to any `nc-<group>` groups they need.
4. Give them a way in. Self-service email needs Authentik SMTP, which is NOT configured on every
   instance (check `AUTHENTIK_EMAIL__HOST`). Without SMTP, issue a one-time link yourself (see "Issue a
   recovery link") and send it over a channel you trust.
5. They open the link once (it signs them in), then Settings, Change password, and add an authenticator
   (MFA, see below). After that they open `https://cloud.example.com` and use the SSO button, or are
   redirected straight to it.
6. First login creates or adopts the Nextcloud account. Verify: `occ user:info <uid>` shows backend
   `user_oidc` for a new account (or the old backend for an adopted one), the right email, and the
   expected groups. There must be exactly one account for that person: check `occ user:list`.
7. Grants inside Nextcloud (Team Folder access, shares, Talk rooms) are still granted once in Nextcloud,
   to the person or, better, to a group.

## Add a household (shared group)

1. Authentik: create group `nc-hh-<name>`. Add the members.
2. Nextcloud group `hh-<name>` appears at the first member's next login. Later members join it at their
   own next login. Nothing to create by hand.
3. Grant the group access once in Nextcloud (a Team Folder group, a share, a Talk room). The grant is
   manual; membership is automatic.
4. The name must match the whitelist regex (`hh-.+` in the example). Otherwise the group is created but
   removals will not sync, and you will have drift.

## Remove a person

1. Authentik: set the user inactive (preferred, reversible), or remove them from `skhub-users`. They can
   no longer start a login. Also remove them from `skflow-operators` for Windmill.
2. Existing Nextcloud browser sessions and app passwords keep working until the deprovision job runs.
   Run it: `v1/scripts/sso-nextcloud-deprovision.py` (dry run by default, `--apply` disables the
   Nextcloud accounts whose Authentik user is inactive or outside `skhub-users`; accounts that have no
   Authentik counterpart, such as the local admin and service accounts, are never touched). Prefer a
   schedule (for example a 15 minute Windmill schedule); until one exists, run it by hand after every
   removal and say so in the change record.
3. Their data stays in Nextcloud. Deleting it (`occ user:delete`), transferring ownership first
   (`occ files:transfer-ownership`), and removing Team Folder grants are deliberate manual steps.
4. Re-enable is also manual: reactivate in Authentik, then `occ user:enable <uid>`.

## Reset a password

Passwords exist only in Authentik. Nextcloud keeps an old local password for SSO users but nobody uses it.

- Self-service ("forgot password" on the Authentik login page): works only when SMTP is configured and
  the brand's recovery flow is set. If SMTP is not configured the link is not shown, on purpose, because
  it would error.
- Admin-assisted: issue a recovery link (next section). The person opens it, is signed in, and sets a new
  password under Settings, Change password.
- App passwords are separate. Resetting the Authentik password does not revoke them. If the reason is a
  suspected compromise, also delete the person's app passwords (Nextcloud, Settings, Security, or
  `occ user:auth-tokens:list <uid>` and `occ user:auth-tokens:delete <uid> <id>`) and kill sessions.

## Issue a recovery link

Run on the node that hosts the Authentik server container. The link is a SECRET: whoever opens it first
becomes that user, so deliver it to the person directly and never paste it into a shared channel or a log.

```bash
C=$(docker ps -q -f name=sksso-prod_server | head -1)
docker exec -i "$C" sh -c 'cat > /tmp/x.py; ak shell < /tmp/x.py; rm /tmp/x.py' <<'PY'
from datetime import timedelta
from django.utils.timezone import now
from authentik.core.models import User
from authentik.recovery.lib import create_recovery_token
user = User.objects.get(username="USERNAME")
token, path = create_recovery_token(user, now() + timedelta(days=2), "operator-issued")
print("LINK https://sso.example.com" + path)
PY
```

Keep the expiry short (days, not weeks). Links are single use. If it was sent to the wrong place, delete
the token in Authentik (Directory, Tokens and App passwords) and issue a new one.

## MFA recommendation

Authentik is now the front door to Nextcloud and Windmill, so a stolen password is a stolen estate.
Require an authenticator (TOTP or WebAuthn) for at least every member of `skadmin` and every Windmill
operator, and add it for everyone at first login. In Authentik: add an Authenticator Validation stage to
the authentication flow with "not configured action" set to configure (or deny, once everyone is enrolled).
Keep the break-glass accounts out of the SSO path so MFA problems cannot lock out recovery.

## App passwords, desktop and mobile clients, WebDAV

Clients that cannot complete a browser SSO login use an app password: Nextcloud, Settings, Security,
Devices and sessions, create a new app password per device, name it after the device. They work with the
desktop sync client, mobile apps, DAVx5, WebDAV mounts and OCS scripts, and they are revoked per device.
Using the SSO login in the desktop client also works (it hands out an app password for you). Never put a
person's real SSO password into a client.

Service accounts (automation users that call WebDAV or OCS) stay local Nextcloud accounts with an app
password or a strong password in the vault, record owner and expiry next to it. They have no Authentik
user and no SSO. Do not set `check-bearer` on the provider: it would make Nextcloud validate Bearer
tokens against the IdP, which service accounts do not use.

## Break-glass

- Nextcloud: `https://cloud.example.com/login?direct=1` shows the local login form even though `/login`
  redirects to Authentik. Keep one local admin with a vault-stored password. `occ` on the node always
  works regardless of SSO.
- Authentik: keep a non-SSO administrator and a documented recovery-key procedure, plus a tested restore
  of the database dump.
- Test both after any SSO change.

## Rotate a client secret

Two halves share one secret per application, so change both in one sitting.

skhub:
1. Generate a new secret (`openssl rand -hex 32`) into the vault (never argv, never a chat).
2. Update the Authentik provider `skhub-nextcloud` client secret (the blueprint reads it from the
   environment variable, so update the sksso env value and redeploy sksso, or edit the provider in the UI).
3. Update Nextcloud: `occ user_oidc:provider "<identifier>" --clientsecret-env=VAR` with the value in the
   environment of a one-off exec.
4. Verify the login redirect (`GET /apps/user_oidc/login/<id>` answers 303 to Authentik) and do one real
   login.

skflow: change `sksso.SKFLOW_OIDC_CLIENT_SECRET` and `skflow.SSO_CLIENT_SECRET` to the same new value and
redeploy sksso, then skflow (the provisioner rewrites the `oauths` setting). Verify
`/api/oauth/list_logins` lists the provider and test a login.

Existing sessions survive a secret rotation.

## Roll back SSO

Each step is independent and reversible; passwords and app passwords were never changed.

Soft rollback, keep SSO but restore the password form as the default:
```bash
occ config:app:set user_oidc allow_multiple_user_backends --value=1
```
(`/login` then shows the form with the SSO button next to it.)

Stop just-in-time creation and group sync:
```bash
occ config:system:set user_oidc auto_provision --type=boolean --value=false
occ user_oidc:provider "<identifier>" --group-provisioning=0
```

Remove SSO from Nextcloud entirely:
```bash
occ user_oidc:provider:delete "<identifier>"
occ app:disable user_oidc
occ config:system:delete user_oidc
```
Accounts created through SSO have no usable local password. Before this step, issue those people a
password with `occ user:resetpassword <uid>` (or give them app passwords) or they cannot log in.
In Authentik detach the provider from the `skhub` application and delete the provider, scope mapping and
group when you are sure.

Windmill: clear the `oauths` global setting and remove the `skflow-windmill` application and provider;
local Windmill users and the superadmin keep working.

## Forward auth: the lesson, and when it is right

Do not put Authentik forward auth in front of Nextcloud, ever (reasons and the path table are in
[sso-nextcloud-oidc.md](sso-nextcloud-oidc.md)). It also does not belong in front of Windmill once native
SSO works: it produces a double login and gates paths that machine clients and the editor websocket need.
For skflow leave `SSO_MIDDLEWARE` empty. Forward auth is right only for an app with no usable login of
its own, as an allow-list: a router for `/outpost.goauthentik.io/` to the Authentik server, a higher
priority bypass router for APIs, webhooks, websockets, health and status paths, `/.well-known/`, agent
protocols and public share links, then the protected router. See the recipe in
[sso-estate-plan.md](sso-estate-plan.md). After removing forward auth from an app, delete its now unused
Authentik proxy application and provider and detach the provider from the embedded outpost, or the
portal shows a duplicate tile.

## Portal tile sync caveat

If you run a tile-sync script that reconciles Authentik applications from a `tiles.yml`, it must list
provider-backed applications (skhub, with its groups) as provider-backed. A tile declared as a plain link
with no provider makes the next run detach the OAuth provider and remove group bindings, which breaks SSO
for everyone. Applications and groups that are not listed in the file must be left alone (new `nc-*`
groups and the Windmill application are examples). Merge the tile fix before anyone re-runs the sync, and
check afterwards that the application still shows its provider.

## Checks after any change here

- `occ user_oidc:provider` lists the provider; `GET /login` answers 302 to the SSO login and
  `GET /login?direct=1` answers 200.
- One real or throwaway-user login lands in exactly one Nextcloud account with the right groups.
- Service-account WebDAV `PROPFIND` still answers 207; `occ notify_push:self-test` passes.
- The local admin can still log in through `?direct=1`.
