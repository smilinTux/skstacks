# SKStacks instance config repo

This is a starting layout for an SKStacks **instance**: a private, per-site
repo that holds your cluster's inventory and encrypted secrets, and pins the
public [`smilinTux/skstacks`](https://github.com/smilinTux/skstacks) framework
as a submodule at a released tag. See
[`v2/docs/INSTANCE-MODEL.md`](https://github.com/smilinTux/skstacks/blob/main/v2/docs/INSTANCE-MODEL.md)
in the main repo for the full framework/instance split this follows (that doc
also covers the newer v2 platforms; this template covers v1, Docker Swarm +
Ansible, the framework's current production path -- see
[Rollout](https://github.com/smilinTux/skstacks) in the design spec for why
v1 comes first).

If you're reading this inside the main `skstacks` repo, this directory
(`templates/config-repo/`) is a fixture: a single public source of truth that
[`scripts/generate-config-template.sh`](https://github.com/smilinTux/skstacks/blob/main/scripts/generate-config-template.sh)
copies verbatim to publish the
[`skstacks-config-template`](https://github.com/smilinTux/skstacks-config-template)
repo, and a CI job
([`.github/workflows/config-template-drift.yml`](https://github.com/smilinTux/skstacks/blob/main/.github/workflows/config-template-drift.yml))
keeps the two from drifting apart. If you're reading this at the root of your
own repo, you started from that template and this is your walkthrough.

## Layout

```
CONFIG_SCHEMA                                     # instance schema version (see below)
v1/
  ansible/
    shared/
      hosts                                       # your inventory (Ansible INI)
    optional/
      group_vars/
        prod/
          <service>-prod_vault.yml.example         # plaintext placeholder reference
          <service>-prod_vault.yml                  # the real file: ansible-vault ciphertext
README.md
```

One `<service>-<env>_vault.yml` per service per environment you deploy
(`dev`, `staging`, `prod`); this template ships one example, `skbook`
(BookStack), under `prod`. `v1/ansible/shared/tasks/select_vault_file.yml` in
the framework also supports a per-domain/per-cluster vault name
(`<service>-<env>-<domain>-<cluster>_vault.yml`, dots in the domain replaced
with dashes) for instances running more than one site; this template uses the
simpler form, which is what it falls back to automatically.

## Config schema

`CONFIG_SCHEMA` at the repo root is a plain integer naming the shape of this
instance repo (which files exist, how vaults are named) that it was written
against. Each framework release's CHANGELOG notes any "Instance action"
needed when this shape changes; that note is your migration guide. Leave this
file at the value this template shipped with unless a framework release tells
you otherwise.

## Use this template

1. On the
   [`skstacks-config-template`](https://github.com/smilinTux/skstacks-config-template)
   repo, click **Use this template -> Create a new repository**. **Choose
   Private.** This repo will hold real (if encrypted) credentials and your
   site's real hostnames; it must never be public.
2. Clone your new repo. Pin the framework as a submodule at a released tag
   (see [the main repo's tags](https://github.com/smilinTux/skstacks/tags)
   for the current list):
   ```
   git submodule add --name framework https://github.com/smilinTux/skstacks.git framework
   git -C framework checkout skstacks-v2.24.0
   git add framework .gitmodules
   git commit -m "pin framework to skstacks-v2.24.0"
   ```
   Bump this (checkout a newer tag, commit) to upgrade later; that commit is
   your upgrade record.
3. Edit `v1/ansible/shared/hosts`: replace the `example-site` group with your
   real cluster name, and the placeholder hosts with your real managers and
   workers.
4. For each service you're deploying, rename
   `v1/ansible/optional/group_vars/prod/skbook-prod_vault.yml.example` to
   `<service>-prod_vault.yml.example`, fill in real values (field reference:
   `v1/ansible/optional/<service>/README.md` in the framework, under
   "Required instance vars (vault)"), then encrypt it (see next step) and
   delete the `.example` copy of any service you don't run, or keep it as
   your own plaintext scratch copy -- just never commit secrets unencrypted.

## Set the vault password

This template's shipped `skbook-prod_vault.yml` is encrypted with a
**documented demo password**, `skstacks-template-demo`, purely so the
render-gate CI job below can decrypt and render it. Replace it with your own
real, private password before you put any real value in a vault:

```
ansible-vault rekey --vault-password-file <old-password-file> \
  --new-vault-password-file <new-password-file> \
  v1/ansible/optional/group_vars/prod/<service>-prod_vault.yml
```

Keep the password file itself **outside** this repo (it is passed to the
deploy playbooks as `--vault-password-file`, never committed).

To re-encrypt after editing a `.example` file:

```
ansible-vault encrypt --vault-password-file <password-file> \
  --output <service>-prod_vault.yml <service>-prod_vault.yml.example
```

## Run the render gate

Before any real deploy, the framework's render gate
(`v1/tests/render/render_playbooks.py` in the main repo) proves your vault
and inventory produce valid output through real Ansible, without touching a
host. With `framework/` checked out alongside this repo and the demo
password file from above:

```
cd framework
python v1/tests/render/render_playbooks.py --service skbook --env prod
```

The same gate runs in this template's own CI
(`.github/workflows/config-template-drift.yml`'s `render` job) against this
template's shipped `skbook` placeholder, so "Use this template" always starts
from a known-good render.

## First deploy

```
ansible-playbook -i v1/ansible/shared/hosts \
  framework/v1/ansible/optional/skbook/deploy_skbook-prod.yml \
  -e skstacks_vault_dir=$(pwd)/v1/ansible \
  --vault-password-file <password-file>
```

`skstacks_vault_dir` points the framework's `select_vault_file.yml` at this
repo's vaults instead of its own (otherwise-empty) `group_vars/`; see
`v1/ansible/shared/tasks/select_vault_file.yml` in the framework for exactly
how it resolves the path. A known gap as of this writing: already-migrated
services also need a local, untracked symlink so the framework checkout's
own relative lookup finds this repo's vaults, for example:

```
ln -s ../../../../../v1/ansible/optional/group_vars \
  framework/v1/ansible/optional/group_vars
```

(adjust the `../` count for your checkout layout). This is tracked for a
real fix; until then, create the symlink once per fresh `framework/`
checkout.

**One-command deploy with automatic rollback:** once
`tools/skstacks-deploy` lands (Gap 2 of the same design, tracked in the
framework's `feat/skstacks-deploy-tool` work), it wraps the lock, snapshot,
playbook-run and verify-or-rollback sequence above into one command. Until
then, use the manual invocation above, dry-run first (`ansible-playbook
--check`), and keep your own pre-deploy backup.

## Upgrade

Bump the `framework` submodule to a newer release tag (see "Use this
template" step 2), commit, re-run the render gate, then redeploy. The commit
is the upgrade record.

## Troubleshooting

- **"vault password file ... not found"**: the `--vault-password-file` you
  passed to `ansible-vault` / `ansible-playbook` doesn't exist at that path,
  or (in a sandboxed shell) stdin/stdout aren't in blocking mode -- if
  `ansible-vault` reports a non-blocking-IO error before the password-file
  error, that's the real cause; run it through a wrapper that sets blocking
  I/O on fds 0-2 first.
- **render gate fails with "missing vars/<service>.example.yml"**: that
  service has no public example fixture yet in the framework's
  `v1/tests/render/vars/`; it isn't covered by the render gate until one is
  added there.
- **a deploy can't find your vault**: confirm `skstacks_vault_dir` points at
  this repo's `v1/ansible/` (not the framework checkout's), and that the
  vault filename matches exactly what `select_vault_file.yml` constructs
  (service, env, and, if you're using the domain/cluster form, your
  inventory's `domain`/`cluster_name` vars with dots replaced by dashes).
