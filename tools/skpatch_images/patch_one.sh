#!/usr/bin/env bash
# Patch one upstream image's fixable OS-package HIGH/CRITICAL CVEs with
# Copacetic and push it as a tag of $SKPATCH_REPO. Writes a result JSON.
#
#   patch_one.sh <image-ref> <result.json>
#
# Env: TRIVY_IMAGE, COPA (path to the copa binary), BUILDKIT_ADDR,
#      SKPATCH_REPO (default ghcr.io/smilintux/skpatched), SKPATCH_PUSH (1/0).
# The tag is derived from the upstream digest and the exact fix list, so an
# unchanged image re-uses the tag it already has instead of making a new one.
set -euo pipefail
ref=$1; out=$2
here=$(cd "$(dirname "$0")" && pwd)
repo=${SKPATCH_REPO:-ghcr.io/smilintux/skpatched}
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
mkdir -p "$HOME/.cache/trivy"

result() { python3 -c 'import json,sys; print(json.dumps(dict(a.split("=",1) for a in sys.argv[1:])))' ref="$ref" "$@" > "$out"; }
scan() {  # scan <ref> <out.json> [trivy args]
  local img=$1 o=$2; shift 2
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME/.cache/trivy:/cache" -v "$work:/work" \
    "$TRIVY_IMAGE" image --cache-dir /cache --quiet --format json -o "/work/$o" \
    --db-repository mirror.gcr.io/aquasec/trivy-db --java-db-repository mirror.gcr.io/aquasec/trivy-java-db \
    --ignore-unfixed --severity HIGH,CRITICAL "$@" "$img"
}

if ! docker pull -q --platform linux/amd64 "$ref" >/dev/null 2>&1; then result status=pull-failed; exit 0; fi
scan "$ref" all.json || { result status=scan-failed; exit 0; }
scan "$ref" os.json --pkg-types os || { result status=scan-failed; exit 0; }
before=$(python3 "$here/report.py" count "$work/all.json")
os_n=$(python3 -c 'import json,sys;c=json.loads(sys.argv[1]);print(c["critical"]+c["high"])' "$(python3 "$here/report.py" count "$work/os.json")")
if [ "$os_n" = 0 ]; then result status=nothing-to-patch before="$before"; exit 0; fi

tag=$(python3 "$here/report.py" tag "$ref" "$(python3 "$here/report.py" key "$work/os.json")")
target="$repo:$tag"
if docker manifest inspect "$target" >/dev/null 2>&1; then
  docker pull -q "$target" >/dev/null
  reused=1
else
  reused=0
  local_tag="skpatch-$tag"
  if ! "$COPA" patch -i "$ref" -r "$work/os.json" -t "$local_tag" --addr "$BUILDKIT_ADDR" --platform linux/amd64 > "$work/copa.log" 2>&1; then
    tail -20 "$work/copa.log" >&2; result status=patch-failed before="$before"; exit 0
  fi
  name=${ref%%@*}; last=${name##*/}; [[ "$last" == *:* ]] && name=${name%:*}
  docker tag "$name:$local_tag" "$target"
fi

docker image inspect "$ref" > "$work/up.inspect.json"
docker image inspect "$target" > "$work/p.inspect.json"
if ! python3 "$here/report.py" config "$work/up.inspect.json" "$work/p.inspect.json" >&2; then result status=config-changed before="$before"; exit 0; fi
scan "$target" patched.json || { result status=scan-failed before="$before"; exit 0; }
after=$(python3 "$here/report.py" count "$work/patched.json")
gain=$(python3 -c 'import json,sys;b,a=json.loads(sys.argv[1]),json.loads(sys.argv[2]);print(int(a["critical"]+a["high"]<b["critical"]+b["high"] and a["critical"]<=b["critical"]))' "$before" "$after")
if [ "$gain" != 1 ]; then result status=no-gain before="$before" after="$after"; exit 0; fi

if [ "$reused" = 0 ] && [ "${SKPATCH_PUSH:-1}" = 1 ]; then docker push -q "$target" >/dev/null; fi
digest=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$target" | grep "^$repo@" | head -1 | cut -d@ -f2)
[ -n "$digest" ] || { result status=no-digest before="$before" after="$after"; exit 0; }
result status=patched image="$target@$digest" reused="$reused" before="$before" after="$after"
