# People provisioning with Authentik and Nextcloud (user_oidc)

Authentik is the source of truth for people. Nextcloud follows. Companion to `sso-nextcloud-oidc.md`. Step by step operator procedures (add a person or household, remove, reset,
recovery links, secret rotation, rollback) are in [sso-user-management.md](sso-user-management.md).

## Model
- One Authentik user per person. Username = the Nextcloud account id (uid). Pin it in user attribute `nextcloud_uid`.
- Access to Nextcloud = membership of Authentik group `skhub-users` (bound to the application).
- Just-in-time provisioning: `user_oidc.auto_provision=true` plus `soft_auto_provision=true`. A first login either
  adopts the existing local account with the same uid (no duplicate) or creates a new one.
- Groups: Authentik groups named `nc-<name>` become Nextcloud group `<name>` through the `nextcloud_groups` claim.
  Only Nextcloud groups matching the provider `groupWhitelistRegex` are added or removed by SSO, so unrelated local
  groups are never changed. Use `ops-team` and `hh-<household>` names for IdP-managed groups.
- Passwords live only in Authentik. Users create app passwords in Nextcloud for WebDAV and desktop or mobile clients.
- Local-only accounts (break-glass admin, service accounts) have no Authentik user and never get SSO.

## Scope mapping (Authentik, scope name `nextcloud`)
```python
user = request.user
uid = user.attributes.get("nextcloud_uid")
if not uid:
    uid = user.username
    user.attributes["nextcloud_uid"] = uid
    user.save(update_fields=["attributes"])
ncgroups = sorted(g.name[3:] for g in user.groups.all() if g.name.startswith("nc-") and len(g.name) > 3)
return {"nextcloud_uid": uid, "nextcloud_groups": ncgroups}
```

## Nextcloud settings
```
occ config:system:set user_oidc auto_provision --type=boolean --value=true
occ user_oidc:provider "<identifier>" --mapping-uid=nextcloud_uid --mapping-groups=nextcloud_groups \
  --group-provisioning=1 '--group-whitelist-regex=^(ops-team|hh-.+)$'
occ config:app:set user_oidc allow_multiple_user_backends --value=0   # /login redirects to the IdP
```
`/login?direct=1` still shows the local form (break-glass).

## Add a person
1. Authentik: create the user (username, name, email), add to `skhub-users` and any `nc-<group>`.
2. Create a one-time link: `ak shell` with `authentik.recovery.lib.create_recovery_token(user, expiry, "note")`
   and prefix the returned path with the SSO origin. The link signs the person in once; they set a password under
   Settings > Change password and add an authenticator. Treat the link as a secret and send it yourself.
3. They open the Nextcloud URL and are signed in.
Self-service email recovery (the "Forgot password?" link on the login page) is enabled and needs SMTP configured in Authentik
(`AUTHENTIK_EMAIL__*`, set via the stack vault keys `sksso.email_*`; the Gmail app password lives only in the vault).
Invites: `POST /api/v3/core/users/<pk>/recovery_email/` with `{"email_stage": "<invite stage>", "token_duration": "days=7"}`.

## Deprovision
Deactivate the Authentik user or remove it from `skhub-users`: no new logins. A disabled Nextcloud account cannot be used
with its sessions or app passwords either (WebDAV with its app password returns 503 once disabled), but the stored app
passwords come back to life if the account is re-enabled. `v1/scripts/sso-nextcloud-deprovision.py --apply` disables
Nextcloud accounts whose Authentik counterpart is inactive or outside `skhub-users` and never touches accounts without
a counterpart. Dry run is the default.

Scheduled: Windmill script `f/ops/sso_deprovision` (workspace `fwe`), schedule every 15 minutes with `apply=true`.
Credentials are Windmill secret variables under `f/ops/`: `authentik_deprovision_token` (Authentik service account
`svc-deprovision`, role with only `authentik_core.view_user`) and `svc_provisioning_app_password` (Nextcloud service
user `svc-provisioning`, group admin: subadmins cannot manage users that are in no group, which is the usual state of
SSO users). Nextcloud refuses `disable` for app passwords ("Password confirmation is required"), so the secret is the
account's long random password, not an app password. Placeholders: `<authentik-token>`, `<svc-provisioning-secret>`.

## Verified without a human
A throwaway Authentik user with a random password drives the flow executor API through
`/apps/user_oidc/login/<id>`; Nextcloud then shows exactly one new account (backend `user_oidc`) with the expected uid
and groups. A second throwaway matched to a pre-created local account stayed on the local backend with no duplicate.
Delete both sides afterwards.
