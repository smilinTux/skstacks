#!/bin/bash
# SKGit global pre-receive backstop: reject pushes that add any blob over a size limit.
# Installed (as a symlink) into every repo's hooks/pre-receive.d/ by install-hooks.sh.
# Runs inside the Forgejo container as the git user. Fast path: one rev-list plus one
# cat-file --batch-check over only the objects the push introduces. LFS content never
# trips it because LFS pointer files are tiny.
#
# Config (sourced, optional): /data/gitea/custom-hooks/size-limit.conf
#   SKGIT_MAX_MIB=25
# Allow-list (optional): /data/gitea/custom-hooks/size-limit.allow, one entry per line:
#   owner/repo            exempt the whole repo
#   owner/repo:glob       exempt matching paths in that repo (shell glob, * matches /)
set -u

HOOK_DIR=${SKGIT_HOOK_DIR:-/data/gitea/custom-hooks}
SKGIT_MAX_MIB=25
# shellcheck disable=SC1091
[ -r "$HOOK_DIR/size-limit.conf" ] && . "$HOOK_DIR/size-limit.conf"
max_bytes=$((SKGIT_MAX_MIB * 1024 * 1024))

repo="${GITEA_REPO_USER_NAME:-}/${GITEA_REPO_NAME:-}"
if [ "$repo" = "/" ]; then
  gd=$(cd "${GIT_DIR:-.}" && pwd)
  repo="$(basename "$(dirname "$gd")")/$(basename "$gd" .git)"
fi
repo_lc=$(printf '%s' "$repo" | tr 'A-Z' 'a-z')

allow_file="$HOOK_DIR/size-limit.allow"
globs=()
if [ -r "$allow_file" ]; then
  while IFS= read -r line; do
    line=${line%%#*}; line=$(printf '%s' "$line" | tr -d ' \t')
    [ -n "$line" ] || continue
    name=${line%%:*}
    [ "$(printf '%s' "$name" | tr 'A-Z' 'a-z')" = "$repo_lc" ] || continue
    if [ "$name" = "$line" ]; then exit 0; fi
    globs+=("${line#*:}")
  done < "$allow_file"
fi

zero=0000000000000000000000000000000000000000
news=()
while read -r old new ref; do
  case "$new" in "$zero"|0000000000000000000000000000000000000000000000000000000000000000) continue ;; esac
  news+=("$new")
done
[ ${#news[@]} -gt 0 ] || exit 0

# New branches, updates and force pushes alike: everything reachable from the pushed tips
# that is not already reachable from an existing ref. Objects sit in the quarantine dir.
offenders=$(git rev-list --objects "${news[@]}" --not --all 2>/dev/null \
  | git cat-file --batch-check='%(objecttype) %(objectsize) %(objectname) %(rest)' 2>/dev/null \
  | awk -v max="$max_bytes" '$1=="blob" && $2+0>max { p=$0; sub(/^[^ ]+ [^ ]+ [^ ]+ ?/,"",p); print $2 "\t" $3 "\t" p }' \
  | sort -u -t"$(printf '\t')" -k2,2)
[ -n "$offenders" ] || exit 0

bad=""
while IFS=$'\t' read -r size sha path; do
  skip=0
  for g in ${globs[@]+"${globs[@]}"}; do
    # shellcheck disable=SC2254
    case "$path" in $g) skip=1; break ;; esac
  done
  [ $skip -eq 1 ] && continue
  bad+=$(printf '  %7.1f MiB  %s  (blob %s)\n' "$(awk -v s="$size" 'BEGIN{print s/1048576}')" "${path:-?}" "${sha:0:10}")$'\n'
done <<< "$offenders"
[ -n "$bad" ] || exit 0

{
  echo ""
  echo "------------------------------------------------------------------"
  echo "PUSH REJECTED by SKGit: file(s) larger than ${SKGIT_MAX_MIB} MiB."
  echo "------------------------------------------------------------------"
  printf '%s' "$bad" | cat
  echo ""
  echo "Big files do not belong in plain git history (they bloat every clone forever)."
  echo "Use Git LFS (git lfs track <pattern>; git lfs migrate import) or the repo's"
  echo "media store, then re-push. If the blob is already in your history, rewrite it"
  echo "out (git lfs migrate import --include=<path> --everything) before pushing."
  echo "Repo ${repo}. A site admin can exempt a repo or path (size-limit.allow on the skgit host)."
} >&2
exit 1
