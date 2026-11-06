# skpatch

**Deploys [linux-update-dashboard](https://github.com/TheDuffman85/linux-update-dashboard) (AGPL-3.0) on the swarm: a self-hosted dashboard that checks, and if armed applies, Linux package updates across a fleet of hosts over SSH.** This is the host-patching half of the wider [skpatch program](../../../../docs/skpatch-program.md); the image-patching half (SBOM scanning, CVE tracking, Renovate) is a separate, mostly-planned pipeline described there. **Maturity tier:** T0, Classical (see "Honest claims"). **Version phase:** framework `v1` tree (this repo's own [Versions table](../../../../README.md#versions): "Published, one service at a time", not the generic ecosystem `VERSION_LIFECYCLE` "Legacy" phase).

Opt-in: nothing renders or changes for an instance that never runs
`deploy_skpatch-<env>.yml`.

## Quickstart

```bash
# 1. Deploy the dashboard (report-only by default: ALLOW_ACTIONS: false)
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skpatch/deploy_skpatch-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass

# 2. Give it something to patch: provision one estate's nodes (separate playbook)
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skpatch/provision_skpatch_nodes.yml \
  -l <estate-inventory-group> -e skpatch_provision_env=prod \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

Full detail (build, test, deploy, configuration, API, troubleshooting)
is in **[SOP.md](./SOP.md)**.

## Honest claims (read this)

- This role **generates and stores classical key material**: one
  dedicated SSH **ed25519** keypair per estate (`ssh-keygen`, standard
  OpenSSH, no custom crypto) and an at-rest **AES-256-GCM** encrypted
  credential store inside the upstream app, keyed by
  `LUDASH_ENCRYPTION_KEY`. Both are standard, off-the-shelf primitives;
  this role implements neither a cipher nor a key-exchange protocol of
  its own.
- It makes **no post-quantum or hybrid-KEM claim anywhere**, and should
  not: everything here is classical (SSH ed25519, TLS for the dashboard's
  own HTTPS). It is not a "crypto component" under
  [`SK_REPO_DOC_STANDARD`](https://github.com/smilinTux/sk-standards/blob/main/standards/SK_REPO_DOC_STANDARD.md)
  in the sense that matters there (it does not implement, negotiate, or
  expose a cryptographic protocol to other systems); it operates one.
  No separate `CRYPTO_SPEC.md` is carried for that reason; this paragraph
  is the compliance statement.
- `ALLOW_ACTIONS` in the vault is **metadata only**: flipping it grants
  nothing by itself. The real enforcement is a sudoers allowlist written
  by `provision_skpatch_nodes.yml` on each target node, gated by that
  node's **own** `skpatch_allow_actions` (see SOP §6).
- **One SSH keypair per estate, never shared, never a reused admin key.**
  A compromise of this container is a compromise of every host it holds
  a key for; see SOP §1 "Why this app gets more scrutiny than most".

## How this fits the program

`skpatch` the role does hosts; `skpatch` the program also covers
container images, via `skbom` (Dependency-Track plus Trivy SBOM scanning)
and Renovate, both run as `skflow` (Windmill) flows and gated through the
same release path as everything else. See
**[`docs/skpatch-program.md`](../../../../docs/skpatch-program.md)** for
the cadence/policy table, the architecture diagrams, and what today's
state actually is (most of the image half is still planned; only the
host-patching role below is live).

## Start here

- **What this is, in two sentences.** A dashboard that holds one SSH
  credential per estate and, only if explicitly armed per node, can run
  package upgrades and reboots across that estate's hosts. By default it
  only reports what is out of date.
- **Key entry points:**
  - [`deploy_skpatch-<env>.yml`](./) deploys the dashboard stack.
  - [`provision_skpatch_nodes.yml`](./provision_skpatch_nodes.yml) is
    idempotent and reversible: it creates the low-privilege account,
    installs the estate's keypair, and writes the sudoers allowlist on
    each target.
  - [`src/skpatch/skpatch.sudoers.j2`](./src/skpatch/skpatch.sudoers.j2)
    is the exact-command allowlist; this is where "report-only" is
    actually enforced, not in the app.
  - [`SOP.md`](./SOP.md) has the full operating procedure (build, test,
    deploy, configuration reference, troubleshooting).
- **The data it owns.** `dashboard.db` (SQLite, under `/data`): every SSH
  credential this instance holds, AES-256-GCM-encrypted at rest.
- **External dependencies & integration points.** Traefik (skfence or
  skfenceha) for ingress; Authentik (sksso) for the default SSO-gated
  sign-in; SSH to whatever nodes `provision_skpatch_nodes.yml` has been
  run against.
- **Maturity tier.** T0, Classical. See "Honest claims" above.

## Configuration knobs (vault)

The full reference (every key, every default) is SOP §6. The ones that
matter most when reading this role for the first time:

| Key | Default | What it controls |
|---|---|---|
| `LUDASH_ENCRYPTION_KEY` | required, no default | AES-256-GCM key for the credential store. Losing it makes every stored credential unreadable. |
| `AUTH_MODE` | `sso` | `sso` puts Authentik forward auth in front of the whole router (default); `local` uses the app's own login only. |
| `ALLOW_ACTIONS` | `false` | Instance-wide **metadata**. A node only gets upgrade/reboot sudoers lines when its own `skpatch_allow_actions` (or, if unset, this) is `true`. |
| `SSH_PUBLIC_KEY` | required by provisioning | Public half of this estate's dedicated keypair. |
| `PROVISION_USER` | `skpatch-ro` | The low-privilege account `provision_skpatch_nodes.yml` creates on each target node. |
| `DATA_NODE` | `""` | Required if `DATA_PATH` is local disk, not shared storage; pins the single dashboard replica there. |

**Per-host actions ceiling:** set `skpatch_allow_actions: false` in the
group_vars of every hypervisor, NAS, or any host that must never be
upgraded or rebooted through this app. That per-host `false` always
wins over the vault's instance-wide `ALLOW_ACTIONS: true`.

## Related projects / See also

- ⬆️ **Depends on:** [skfence / skfenceha](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#skfence), Traefik ingress every router here assumes; [sksso](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#sksso), Authentik, for the default `AUTH_MODE: sso`.
- ⬇️ **Part of:** [the skpatch program](../../../../docs/skpatch-program.md): this role is the host-patching half; the image-patching half is `skbom` plus `skflow` below.
- ↔️ **Sibling:** [skbom](../../../../docs/skpatch-program.md#2-components), Dependency-Track, the image-side CVE register this program's other half feeds.
- ↔️ **Sibling:** [skflow](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#skflow), Windmill, which runs the program's `f/skpatch`/`f/skrelease`/`f/ops` automation flows.
- ↔️ **Sibling:** [sksec-web](../sksec-web/), another SSO-gated single-purpose ops dashboard with the same read-only-by-default, actions-as-explicit-opt-in shape.
- ↔️ **Sibling:** [skgit](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#skgit), the repo of record the program's Renovate PRs and release gate target.
- 📐 **Standards:** [sk-standards](https://github.com/smilinTux/sk-standards), the doc/SOP, architecture & data-flow, and cryptography standards this README and its SOP follow.

*License: this role is Apache-2.0 (the framework's own license). The
upstream application it deploys, `linux-update-dashboard`, is AGPL-3.0
and is referenced by image reference only: nothing from it is vendored.*
