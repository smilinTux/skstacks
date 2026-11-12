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
#   vmalert-rules : files <anything>/configs/rules-<class> (the render gate) and
#            <anything>/vmalert-rules/<class>.yml (render_rules.py)
#            -> vmalert -dryRun -rule=<file>
#   vmalert-tests : files <anything>/vmalert-rules/tests/*_test.yml (render_rules.py)
#            -> vmalert-tool unittest --files=<file> --external.label=estate=example,
#            with the vmalert-rules directory mounted so `rule_files: [../<class>.yml]`
#            resolves to the rendered rules (estate "example" is what render_rules.py
#            renders with and what the deployed vmalert adds as its external label).
#   amtool : files <anything>/configs/alertmanager -> amtool check-config (the Alertmanager
#            image's own amtool, entrypoint overridden)
#   alertmanager-inhibition : the first such file is also run for real: the pinned
#            Alertmanager on a free loopback port, a ZfsPoolNotOnline (storage, critical)
#            posted with a ServiceBelowReplicas and an HttpEndpointDown in the same estate,
#            and both consequences must show a non-empty status.inhibitedBy within 30 s.
#            The receivers point at a bridge that does not exist here: delivery fails,
#            inhibition still computes. The container is removed on exit.
# To add a kind (promtool): add a pin lookup and a
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

# check_rule_tests: vmalert-tool unit tests, with the rendered rules one directory up.
check_rule_tests() {
  local files=()
  while IFS= read -r f; do files+=("$f"); done < <(find "$root" -path "$root/ansible" -prune -o -type f -path '*/vmalert-rules/tests/*_test.yml' -print | sort)
  if [ "${#files[@]}" -eq 0 ]; then echo "skip: vmalert-tests not rendered"; return 0; fi
  local image; image="$(image_ref vmalert_tool)"
  for f in "${files[@]}"; do
    echo "check: vmalert-tests $f"
    if ! docker run --rm -v "$(dirname "$(dirname "$f")"):/cfg:ro" "$image" \
        unittest --files="/cfg/tests/$(basename "$f")" --external.label=estate=example; then
      echo "FAIL: vmalert-tests failed for $f" >&2
      exit 1
    fi
  done
}

# check_amtool: amtool check-config over each rendered Alertmanager config.
check_amtool() {
  local files=()
  while IFS= read -r f; do files+=("$f"); done < <(find "$root" -path "$root/ansible" -prune -o -type f ! -name '*.j2' -path '*/configs/alertmanager' -print | sort)
  if [ "${#files[@]}" -eq 0 ]; then echo "skip: amtool not rendered"; return 0; fi
  local image; image="$(image_ref alertmanager)"
  for f in "${files[@]}"; do
    echo "check: amtool $f"
    if ! docker run --rm --entrypoint amtool -v "$(dirname "$f"):/cfg:ro" "$image" check-config "/cfg/$(basename "$f")"; then
      echo "FAIL: amtool check-config failed for $f" >&2
      exit 1
    fi
  done
}

# check_inhibition: see the header. Runs once, on the first rendered Alertmanager config.
check_inhibition() {
  local f; f="$(find "$root" -path "$root/ansible" -prune -o -type f ! -name '*.j2' -path '*/configs/alertmanager' -print | sort | head -n 1)"
  if [ -z "$f" ]; then echo "skip: alertmanager-inhibition not rendered"; return 0; fi
  local image; image="$(image_ref alertmanager)"
  echo "check: alertmanager-inhibition $f"
  # A subshell, so the EXIT trap fires on every way out (set -e abort, exit 1, normal
  # return) and the container never outlives the check. Its status is the check's status.
  if ! (
    set -euo pipefail
    name="skmon-inhibit-$$"
    trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
    timeout 120 docker run -d --name "$name" -p 127.0.0.1::9093 \
      -v "$f:/etc/alertmanager/config.yml:ro" "$image" \
      --config.file=/etc/alertmanager/config.yml --storage.path=/tmp/am >/dev/null
    port="$(docker port "$name" 9093/tcp | head -n 1 | sed 's/.*://')"
    if ! python3 - "$port" <<'PY'
import json, sys, time, urllib.request

base = f"http://127.0.0.1:{sys.argv[1]}/api/v2/alerts"
base_labels = {"estate": "x"}
alerts = [
    {"alertname": "ZfsPoolNotOnline", "class": "storage", "severity": "critical", "node": "n1"},
    {"alertname": "ServiceBelowReplicas", "class": "swarm", "severity": "warning", "service": "s1"},
    # Rule 1 needs a critical source: a drain warning on n2 must not mute n2's storage alert,
    # the not-ready critical on n3 must mute n3's.
    {"alertname": "SwarmNodeNotReady", "class": "node", "severity": "warning", "node": "n2"},
    {"alertname": "ZfsPoolNotOnline", "class": "storage", "severity": "critical", "node": "n2", "pool": "p2"},
    {"alertname": "SwarmNodeNotReady", "class": "node", "severity": "critical", "node": "n3"},
    {"alertname": "ZfsPoolNotOnline", "class": "storage", "severity": "critical", "node": "n3", "pool": "p3"},
    {"alertname": "HttpEndpointDown", "class": "http", "severity": "critical", "probe": "p1",
     "instance": "https://example.test", "forward_auth": "false"},
]
body = json.dumps([{"labels": {**base_labels, **a}} for a in alerts]).encode()


def call(data=None):
    req = urllib.request.Request(base, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r) if data is None else None


deadline = time.time() + 30
while True:
    try:
        call(body)
        break
    except OSError:
        if time.time() > deadline:
            sys.exit("alertmanager did not come up")
        time.sleep(0.5)
want = {"ServiceBelowReplicas", "HttpEndpointDown"}
while True:
    cur = call()
    got = {a["labels"]["alertname"]: a["status"]["inhibitedBy"] for a in cur}
    by_pool = {a["labels"].get("pool"): a["status"]["inhibitedBy"] for a in cur if a["labels"]["alertname"] == "ZfsPoolNotOnline"}
    if all(got.get(n) for n in want) and by_pool.get("p3") and by_pool.get("p2") == []:
        print("inhibited:", sorted(want), "and the critical node alert's storage alert, not the drain warning's")
        sys.exit(0)
    if time.time() > deadline:
        sys.exit(f"not inhibited within 30 s: {got}")
    time.sleep(1)
PY
    then
      docker logs "$name" 2>&1 | tail -n 20 >&2 || true
      exit 1
    fi
  ); then
    echo "FAIL: inhibition check failed for $f" >&2
    exit 1
  fi
}

check_files alloy alloy -name 'agent.alloy' -- validate --stability.level=experimental @FILE@
check_files loki loki -path '*/configs/loki' -- -verify-config -config.file=@FILE@
check_files vmalert-rules vmalert -path '*/configs/rules-*' -- -dryRun -rule=@FILE@
check_files vmalert-rules vmalert -path '*/vmalert-rules/*.yml' ! -path '*/tests/*' -- -dryRun -rule=@FILE@
check_rule_tests
check_amtool
check_inhibition
echo "ok: all rendered configs validated"
