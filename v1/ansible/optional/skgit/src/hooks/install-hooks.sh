#!/bin/bash
# Idempotently link the SKGit global pre-receive hooks into every Forgejo repository.
# Runs on the host that holds /var/data (NFS), as root, from a systemd timer, because
# Forgejo rewrites a repo's hooks/ when the repo is created or hooks are re-synced and
# only ever manages its own pre-receive.d/gitea file. Symlink targets are container
# paths (/data/gitea/custom-hooks/...) and resolve inside the Forgejo container.
set -eu
DATA=${SKGIT_DATA:-/var/data/skgit-prod/data}
REPOS=${SKGIT_REPOS:-$DATA/forgejo/forgejo-repositories}
TARGET=/data/gitea/custom-hooks/size-limit.sh
NAME=10-size-limit
changed=0
for repo in "$REPOS"/*/*.git; do
  [ -d "$repo/hooks" ] || continue
  d="$repo/hooks/pre-receive.d"
  # No Forgejo pre-receive wrapper yet (repo still being created): try next run.
  [ -x "$repo/hooks/pre-receive" ] || continue
  if [ "$(readlink "$d/$NAME" 2>/dev/null || true)" != "$TARGET" ]; then
    mkdir -p "$d"
    ln -sfn "$TARGET" "$d/$NAME"
    chown -h --reference="$repo/hooks/pre-receive" "$d/$NAME" 2>/dev/null || true
    changed=$((changed + 1))
  fi
done
[ "$changed" -eq 0 ] || echo "skgit-install-hooks: linked $changed repo(s)"
