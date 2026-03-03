#!/usr/bin/env bash
# longhorn-preflight.sh — Verify all cluster nodes meet Longhorn prerequisites.
#
# Usage:
#   ./scripts/longhorn-preflight.sh [OPTIONS]
#
# Options:
#   -i, --inventory FILE   Ansible inventory YAML (default: ansible/inventory.yml)
#   -u, --user USER        SSH user override (default: ansible_user from inventory)
#   -k, --key FILE         SSH private key file override
#   --min-disk GB          Minimum free GB required on /var/lib/longhorn (default: 50)
#   -h, --help             Show this help
#
# Checks performed on every node:
#   [REQUIRED] open-iscsi / iscsi-initiator-utils installed
#   [REQUIRED] iscsid service active
#   [REQUIRED] Kernel >= 5.4
#   [REQUIRED] >= MIN_DISK_GB free on /var/lib/longhorn (or nearest parent)
#   [WARN]     multipathd active without Longhorn blacklist (disk conflict risk)
#   [WARN]     nfs-common / nfs-utils missing (needed for NFS backup targets)
#
# Exit codes:
#   0  All nodes passed all REQUIRED checks
#   1  One or more nodes failed a REQUIRED check
#
# Example — run against a custom inventory with a non-default SSH key:
#   ./scripts/longhorn-preflight.sh -i ansible/inventory.yml -k ~/.ssh/cluster_ed25519

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INVENTORY="${SCRIPT_DIR}/../ansible/inventory.yml"
SSH_USER_OVERRIDE=""
SSH_KEY_OVERRIDE=""
MIN_DISK_GB=50

# Colours
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

overall_rc=0

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
log_pass() { echo -e "  ${GREEN}[PASS]${NC} $*"; }
log_warn() { echo -e "  ${YELLOW}[WARN]${NC} $*"; }
log_fail() { echo -e "  ${RED}[FAIL]${NC} $*"; overall_rc=1; }
log_info() { echo -e "        ${CYAN}↳${NC}  $*"; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
usage() {
  grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//'
  exit 0
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -i|--inventory) INVENTORY="$2"; shift 2 ;;
      -u|--user)      SSH_USER_OVERRIDE="$2"; shift 2 ;;
      -k|--key)       SSH_KEY_OVERRIDE="$2"; shift 2 ;;
      --min-disk)     MIN_DISK_GB="$2"; shift 2 ;;
      -h|--help)      usage ;;
      *) echo "Unknown option: $1" >&2; echo "Run with -h for help." >&2; exit 1 ;;
    esac
  done
}

# ---------------------------------------------------------------------------
# Inventory parsing (requires python3 + PyYAML)
# ---------------------------------------------------------------------------
check_python_deps() {
  if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is required to parse the inventory YAML." >&2
    exit 1
  fi
  if ! python3 -c "import yaml" 2>/dev/null; then
    echo "ERROR: PyYAML is required." >&2
    echo "       Install: pip install pyyaml  OR  apt install python3-yaml" >&2
    exit 1
  fi
}

# Outputs tab-separated lines: name<TAB>ip<TAB>user<TAB>key_path
get_hosts() {
  python3 - "$INVENTORY" <<'PYEOF'
import sys, yaml

with open(sys.argv[1]) as fh:
    inv = yaml.safe_load(fh)

seen = {}

def walk(node, inherited=None):
    if inherited is None:
        inherited = {}
    if not isinstance(node, dict):
        return
    level_vars = {**inherited, **node.get("vars", {})}
    for hname, hdata in (node.get("hosts") or {}).items():
        merged = {**level_vars, **(hdata or {})}
        if hname not in seen:
            seen[hname] = {
                "ip":   merged.get("ansible_host", hname),
                "user": merged.get("ansible_user", "root"),
                "key":  merged.get("ansible_ssh_private_key_file", ""),
            }
    for gdata in (node.get("children") or {}).values():
        walk(gdata or {}, level_vars)

walk(inv.get("all", inv))

for hname, info in seen.items():
    print(f"{hname}\t{info['ip']}\t{info['user']}\t{info['key']}")
PYEOF
}

# ---------------------------------------------------------------------------
# Safe key=value parser (avoids eval on remote output)
# ---------------------------------------------------------------------------
get_kv() {
  local key="$1"
  local data="$2"
  echo "$data" | grep "^${key}=" | head -1 | cut -d= -f2-
}

# ---------------------------------------------------------------------------
# Per-node check
# ---------------------------------------------------------------------------
check_node() {
  local name="$1"
  local ip="$2"
  local inv_user="$3"
  local inv_key="$4"

  local user="${SSH_USER_OVERRIDE:-$inv_user}"
  local key="${SSH_KEY_OVERRIDE:-$inv_key}"

  local ssh_opts="-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes"
  [[ -n "$key" ]] && ssh_opts="$ssh_opts -i $key"

  echo ""
  echo -e "=== Node: ${CYAN}${name}${NC} (${ip}) ==="

  # ---- connectivity --------------------------------------------------------
  if ! ssh $ssh_opts "${user}@${ip}" "echo ok" &>/dev/null; then
    log_fail "SSH connection failed (${user}@${ip})"
    log_info "Check SSH key, user, and that the node is reachable."
    return
  fi
  log_pass "SSH connectivity"

  # ---- remote checks (single SSH session) ----------------------------------
  local raw
  raw=$(ssh $ssh_opts "${user}@${ip}" 'bash -s' <<'REMOTE' 2>&1 || true
set -uo pipefail

# --- OS family detection ---
os_family=unknown
if [[ -f /etc/os-release ]]; then
  # shellcheck source=/dev/null
  . /etc/os-release
  case "${ID_LIKE:-} ${ID:-}" in
    *rhel*|*fedora*|*centos*) os_family=rhel    ;;
    *debian*|*ubuntu*)         os_family=debian  ;;
  esac
fi
echo "os_family=${os_family}"

# --- open-iscsi installed ---
iscsi_pkg=0
iscsi_pkg_name=unknown
if [[ "$os_family" == rhel ]]; then
  rpm -q iscsi-initiator-utils &>/dev/null && iscsi_pkg=1 && iscsi_pkg_name=iscsi-initiator-utils
elif [[ "$os_family" == debian ]]; then
  dpkg -l open-iscsi 2>/dev/null | grep -q "^ii" && iscsi_pkg=1 && iscsi_pkg_name=open-iscsi
else
  if rpm -q iscsi-initiator-utils &>/dev/null; then
    iscsi_pkg=1; iscsi_pkg_name=iscsi-initiator-utils
  elif dpkg -l open-iscsi 2>/dev/null | grep -q "^ii"; then
    iscsi_pkg=1; iscsi_pkg_name=open-iscsi
  fi
fi
echo "iscsi_pkg=${iscsi_pkg}"
echo "iscsi_pkg_name=${iscsi_pkg_name}"

# --- iscsid service active ---
iscsid_active=0
systemctl is-active --quiet iscsid 2>/dev/null && iscsid_active=1
echo "iscsid_active=${iscsid_active}"

# --- kernel version ---
kernel=$(uname -r)
echo "kernel=${kernel}"
kmajor=$(echo "$kernel" | cut -d. -f1)
kminor=$(echo "$kernel" | cut -d. -f2 | grep -o '^[0-9]*')
kernel_ok=0
if [[ "$kmajor" -gt 5 ]] || [[ "$kmajor" -eq 5 && "${kminor:-0}" -ge 4 ]]; then
  kernel_ok=1
fi
echo "kernel_ok=${kernel_ok}"

# --- disk space on /var/lib/longhorn (or nearest existing parent) ---
check_path=/var/lib/longhorn
while [[ ! -d "$check_path" && "$check_path" != "/" ]]; do
  check_path=$(dirname "$check_path")
done
free_kb=$(df -k "$check_path" 2>/dev/null | awk 'NR==2{print $4}')
total_kb=$(df -k "$check_path" 2>/dev/null | awk 'NR==2{print $2}')
free_gb=$(( ${free_kb:-0} / 1048576 ))
total_gb=$(( ${total_kb:-0} / 1048576 ))
echo "disk_path=${check_path}"
echo "disk_free_gb=${free_gb}"
echo "disk_total_gb=${total_gb}"

# --- multipathd ---
multipath_active=0
systemctl is-active --quiet multipathd 2>/dev/null && multipath_active=1
echo "multipath_active=${multipath_active}"
multipath_blacklisted=0
if [[ -f /etc/multipath.conf ]] && grep -qE 'devnode|wwid' /etc/multipath.conf 2>/dev/null; then
  multipath_blacklisted=1
fi
echo "multipath_blacklisted=${multipath_blacklisted}"

# --- nfs utils ---
nfs_ok=0
if [[ "$os_family" == rhel ]]; then
  rpm -q nfs-utils &>/dev/null && nfs_ok=1
elif [[ "$os_family" == debian ]]; then
  dpkg -l nfs-common 2>/dev/null | grep -q "^ii" && nfs_ok=1
else
  (rpm -q nfs-utils &>/dev/null || dpkg -l nfs-common 2>/dev/null | grep -q "^ii") && nfs_ok=1
fi
echo "nfs_ok=${nfs_ok}"
REMOTE
  )

  # ---- parse results -------------------------------------------------------
  local os_family; os_family=$(get_kv os_family "$raw")
  local iscsi_pkg;  iscsi_pkg=$(get_kv iscsi_pkg "$raw")
  local iscsi_pkg_name; iscsi_pkg_name=$(get_kv iscsi_pkg_name "$raw")
  local iscsid_active; iscsid_active=$(get_kv iscsid_active "$raw")
  local kernel; kernel=$(get_kv kernel "$raw")
  local kernel_ok; kernel_ok=$(get_kv kernel_ok "$raw")
  local disk_path; disk_path=$(get_kv disk_path "$raw")
  local disk_free_gb; disk_free_gb=$(get_kv disk_free_gb "$raw")
  local disk_total_gb; disk_total_gb=$(get_kv disk_total_gb "$raw")
  local multipath_active; multipath_active=$(get_kv multipath_active "$raw")
  local multipath_blacklisted; multipath_blacklisted=$(get_kv multipath_blacklisted "$raw")
  local nfs_ok; nfs_ok=$(get_kv nfs_ok "$raw")

  log_info "OS: ${os_family:-unknown}"

  # REQUIRED: open-iscsi
  if [[ "${iscsi_pkg:-0}" == "1" ]]; then
    log_pass "open-iscsi package installed (${iscsi_pkg_name})"
  else
    log_fail "open-iscsi / iscsi-initiator-utils NOT installed"
    log_info "Debian/Ubuntu : apt install open-iscsi"
    log_info "RHEL/Rocky    : dnf install iscsi-initiator-utils"
  fi

  # REQUIRED: iscsid service
  if [[ "${iscsid_active:-0}" == "1" ]]; then
    log_pass "iscsid service is active"
  else
    if [[ "${iscsi_pkg:-0}" == "1" ]]; then
      log_fail "iscsid service is NOT running"
      log_info "Fix: systemctl enable --now iscsid"
    else
      log_fail "iscsid cannot be checked (package not installed)"
    fi
  fi

  # REQUIRED: kernel version
  if [[ "${kernel_ok:-0}" == "1" ]]; then
    log_pass "Kernel ${kernel:-unknown} satisfies >= 5.4"
  else
    log_fail "Kernel ${kernel:-unknown} is too old (Longhorn requires >= 5.4)"
    log_info "Upgrade the OS kernel before deploying Longhorn."
  fi

  # REQUIRED: disk space
  if [[ -n "${disk_free_gb}" && "${disk_free_gb}" -ge "$MIN_DISK_GB" ]]; then
    log_pass "Disk: ${disk_free_gb} GB free / ${disk_total_gb} GB total on ${disk_path:-/var/lib/longhorn}"
  else
    log_fail "Insufficient disk: ${disk_free_gb:-0} GB free on ${disk_path:-/var/lib/longhorn} (need >= ${MIN_DISK_GB} GB)"
    log_info "Data path     : /var/lib/longhorn (defaultDataPath)"
    log_info "Minimum       : ${MIN_DISK_GB} GB free for development"
    log_info "Recommended   : 200 GB free per worker for production (replicas=3)"
    log_info "Use --min-disk N to adjust the threshold."
  fi

  # WARN: multipathd conflict
  if [[ "${multipath_active:-0}" == "1" ]]; then
    if [[ "${multipath_blacklisted:-0}" == "1" ]]; then
      log_pass "multipathd active — blacklist configuration found"
    else
      log_warn "multipathd is active without a device blacklist"
      log_info "Longhorn block devices should be excluded from multipath."
      log_info "Add to /etc/multipath.conf:"
      log_info "  blacklist { devnode \"^sd[a-z]+[0-9]*\" }"
      log_info "Then: systemctl restart multipathd"
    fi
  else
    log_pass "multipathd not active (no conflict risk)"
  fi

  # WARN: nfs utils
  if [[ "${nfs_ok:-0}" == "1" ]]; then
    log_pass "nfs-common / nfs-utils installed (NFS backup targets supported)"
  else
    log_warn "nfs-common / nfs-utils not installed"
    log_info "Required only if you set backupTarget to an NFS share."
    log_info "Debian/Ubuntu : apt install nfs-common"
    log_info "RHEL/Rocky    : dnf install nfs-utils"
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  parse_args "$@"

  echo "Longhorn Pre-Flight Check"
  echo "========================="
  echo "Inventory  : $INVENTORY"
  echo "Min disk   : ${MIN_DISK_GB} GB free required on /var/lib/longhorn"
  echo "Data path  : /var/lib/longhorn  (defaultDataPath)"
  echo "Replicas   : 3  (each 1 GB usable = 3 GB raw across nodes)"

  if [[ ! -f "$INVENTORY" ]]; then
    echo ""
    echo "ERROR: Inventory not found: $INVENTORY" >&2
    echo "       Copy ansible/inventory.example.yml → ansible/inventory.yml and edit it." >&2
    exit 1
  fi

  check_python_deps

  local hosts
  hosts=$(get_hosts)

  if [[ -z "$hosts" ]]; then
    echo "ERROR: No hosts found in inventory: $INVENTORY" >&2
    exit 1
  fi

  local node_count=0
  while IFS=$'\t' read -r name ip user inv_key; do
    check_node "$name" "$ip" "$user" "$inv_key"
    (( node_count++ )) || true
  done <<< "$hosts"

  echo ""
  echo "========================="
  if [[ "$overall_rc" -eq 0 ]]; then
    echo -e "${GREEN}All ${node_count} node(s) passed Longhorn pre-flight checks.${NC}"
    echo "You may now deploy Longhorn via the RKE2 auto-manifest:"
    echo "  cp manifests/longhorn.yaml /var/lib/rancher/rke2/server/manifests/"
  else
    echo -e "${RED}Pre-flight checks FAILED on one or more nodes.${NC}"
    echo "Resolve all [FAIL] items before deploying Longhorn."
    echo ""
    echo "Quick-fix reference:"
    echo "  Debian/Ubuntu : apt install open-iscsi nfs-common"
    echo "                  systemctl enable --now iscsid"
    echo "  RHEL/Rocky    : dnf install iscsi-initiator-utils nfs-utils"
    echo "                  systemctl enable --now iscsid"
  fi

  exit "$overall_rc"
}

main "$@"
