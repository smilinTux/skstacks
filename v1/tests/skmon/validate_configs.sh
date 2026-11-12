#!/usr/bin/env bash
# Validate rendered skmon configs with each tool's own validator, run in the
# container image pinned in pins.yml.
#
# usage: validate_configs.sh <rendered_dir>
#   <rendered_dir> is the tree `render_playbooks.py --service skmon --keep <dir>`
#   writes, plus what `render_alloy_variants.py <dir>` adds (the render gate does
#   not render agent.yml, an imported playbook). The script searches the tree and
#   validates a kind of file only when files of that kind are present; otherwise
#   it prints "skip: <kind> not rendered". It exits non-zero on the first failure
#   and names the file.
#
# Only rendered output is validated: the source tree the render gate keeps under
# <rendered_dir>/ansible/, and any *.j2 template anywhere, are never fed to a tool.
#
# File-discovery rules (one `find` pattern per kind; later tasks append kinds):
#   alloy  : files named agent.alloy        -> alloy validate --stability.level=experimental <file>
#            (the agent runs at that level for the loki.write WAL block)
#   loki   : files <anything>/configs/loki  -> loki -verify-config -config.file=<file>
#            (Swarm config source files are written under a configs/ directory,
#            named after the config key)
# To add a kind (vmalert rules, amtool, promtool): add a pin lookup and a
# check_<kind> function below, then call it in the list at the bottom.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PINS="$HERE/../../ansible/optional/skmon/src/config/skmon/pins.yml"

root="${1:?usage: validate_configs.sh <rendered_dir>}"
[ -d "$root" ] || { echo "not a directory: $root" >&2; exit 2; }
root="$(cd "$root" && pwd)"

# image_ref <pin key>: repo:tag@digest from pins.yml
image_ref() {
  python3 -c '
import sys, yaml
p = yaml.safe_load(open(sys.argv[1]))["images"][sys.argv[2]]
print(p["repo"] + ":" + p["tag"] + "@" + p["digest"])' "$PINS" "$1"
}

# run_in <image> <file> <args...>: run the image with the file's directory mounted
# read-only at /cfg; the file is passed to the tool as /cfg/<name>.
run_in() {
  local image="$1" file="$2"; shift 2
  docker run --rm -v "$(dirname "$file"):/cfg:ro" "$image" "${@//@FILE@//cfg/$(basename "$file")}"
}

check_files() {  # check_files <kind> <pin key> <find args...> -- <tool args with @FILE@>
  local kind="$1" key="$2"; shift 2
  local find_args=() tool_args=()
  while [ "$1" != "--" ]; do find_args+=("$1"); shift; done
  shift; tool_args=("$@")
  local files=()
  while IFS= read -r f; do files+=("$f"); done < <(find "$root" -path "$root/ansible" -prune -o -type f ! -name '*.j2' "${find_args[@]}" -print | sort)
  if [ "${#files[@]}" -eq 0 ]; then echo "skip: $kind not rendered"; return 0; fi
  local image; image="$(image_ref "$key")"
  for f in "${files[@]}"; do
    echo "check: $kind $f"
    if ! run_in "$image" "$f" "${tool_args[@]}"; then
      echo "FAIL: $kind validation failed for $f" >&2
      exit 1
    fi
  done
}

check_files alloy alloy -name 'agent.alloy' -- validate --stability.level=experimental @FILE@
check_files loki loki -path '*/configs/loki' -- -verify-config -config.file=@FILE@
echo "ok: all rendered configs validated"
