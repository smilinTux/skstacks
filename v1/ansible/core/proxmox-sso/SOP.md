# proxmox-sso - Standard Operating Procedures

Operational procedures for Proxmox VE hosts standardized on Authentik groups-claim
SSO (see `README.md` for architecture + vars). These three procedures are the ones
an operator actually runs; everything else is `ansible-playbook` + the checker.

## 1. Add a user / grant admin

Access is driven entirely by **Authentik group membership** - never by editing a
Proxmox host directly.

1. In Authentik, add the user to the `skadmin` (full admin), `skeditor`
   (`PVEVMAdmin` - manage VMs/containers, no datacenter-level config), or
   `skreadonly` (`PVEAuditor` - view only) group.
2. The user logs in to the Proxmox host via "Login with Authentik" (or re-logs in
   if already signed in elsewhere in the SSO session).
3. `groups-overwrite 1` means PVE reconciles that user's `*-authentik` group
   membership **on every login** from the `groups` claim - no second step, no
   per-host account creation.
4. Verify: `pveum user permissions <user>@authentik --path /` on the host - the
   permission count should match the granted role (Administrator = 47,
   PVEVMAdmin/PVEAuditor = fewer; compare against another user with the same
   grant for a sanity count rather than memorizing the exact number).

To revoke: remove the user from the Authentik group. Effective on their next
login (immediately, if `groups-overwrite` already stripped the old PVE group
membership - PVE does not pre-emptively log the user out of an active session).

## 2. Break-glass: `root@pam`

`root@pam` is **never SSO-dependent and never removed**. It is the fallback when
Authentik, the network path to it, or a user's own SSO identity is unavailable.

- Login: the Proxmox web UI realm selector → `Linux PAM standard authentication`,
  username `root`, the host's local root password.
- The `proxmox-sso` Ansible role asserts `root@pam` exists before any write and
  refuses to proceed if it's missing - this is defense in depth, not the only
  safeguard. Treat a failed assertion as a stop-everything incident, not a task
  to work around.
- Never bind `root@pam`'s password to anything SSO-related (vault, Authentik, or
  otherwise) - if it depends on the SSO stack being healthy, it is not break-glass.

## 3. Adding a new Proxmox host

1. In Authentik (API or UI - see `optional/sksso/README.md` for the instance
   itself), create a new OAuth2/OIDC **provider** (scope mappings: `openid`,
   `profile`, `email`, and `groups` - copy the working `groups` scope mapping
   from an existing Proxmox provider rather than recreating it) and
   **application** bound to it. Read back the `client_id` + `client_secret`.
2. Pick `username-claim`: use the value that yields `<authentik-username>@authentik`
   as the PVE userid for a **new** host (check which claim the existing hosts'
   Authentik instance actually populates that way - do not guess; the two
   existing hosts intentionally differ: `username` vs `subject`, because
   switching either now would orphan its existing users). `subject` is the
   generally-recommended default for a brand-new host since it is the one OIDC
   claim every provider is guaranteed to emit.
3. Add the host to your private inventory under `[proxmox_sso]`
   (see `README.md` §6 - never commit a real hostname here).
4. Add a per-host vault entry with `proxmox_sso_issuer_url`,
   `proxmox_sso_client_id`, `proxmox_sso_client_secret` (vault only), and
   `proxmox_sso_username_claim`.
5. Dry run first: `ansible-playbook ... deploy_proxmox-sso.yml -l <new-host> --check`
   - the realm doesn't exist yet, so expect a "would create the realm" plan with
   no diff on `/etc/pve` (the realm-creation task is itself skipped under
   `--check`, by design - a genuinely new realm is created, not just aligned, so
   check mode only confirms the role *would* run, not a diff of existing state).
6. Apply: drop `--check`. The role creates the realm, then aligns it to the same
   groups-claim SSO standard as every other host in one pass.
7. Verify: `pveum user permissions <you>@authentik --path /` on the new host, and
   confirm `root@pam` is still a usable fallback before calling the host done.
