# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# lib.sh: shared helpers for the host backup scripts (sync, restic, check,
# predeploy). Sourced, never run. Every instance value comes from the config
# file (BACKUP_CONF); nothing here names a host, pool or brand. The brand
# (BRAND_*) is part of the config too: this engine prints only the product
# name, alert prefix and footer the config gives it.
#
# Lifted from a storage-host setup proven in production (ZFS on a Proxmox
# NAS); the helpers keep their names and behaviour.

BACKUP_CONF="${BACKUP_CONF:-}"

# bash 5.2 would expand "&" in a ${var//pattern/replacement} replacement.
shopt -u patsub_replacement 2>/dev/null || true

skb_log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
skb_die() { skb_log "FATAL: $*" >&2; exit 1; }

# Load the config and apply defaults for optional keys.
skb_load_conf() {
  if [ -z "$BACKUP_CONF" ] || [ ! -r "$BACKUP_CONF" ]; then
    skb_die "config not readable: ${BACKUP_CONF:-<unset BACKUP_CONF>}"
  fi
  # shellcheck disable=SC1090
  . "$BACKUP_CONF"
  : "${SRC_DATASET:?SRC_DATASET missing in $BACKUP_CONF}"
  : "${BRAND_SHORT:?BRAND_SHORT missing in $BACKUP_CONF}"
  : "${BRAND_PRODUCT:=$BRAND_SHORT}"
  : "${BRAND_TAGLINE:=}"
  : "${BRAND_ALERT_PREFIX:=$BRAND_PRODUCT}"
  : "${BRAND_FOOTER:=}"
  : "${BRAND_CONTACT:=}"
  : "${BRAND_LOGO_URL:=}"
  : "${BRAND_CREDITS_TEXT:=}"
  : "${BAK_ROOT:=}"
  : "${APPS_FILE:=}"
  : "${HOOKS_FILE:=}"
  : "${STATE_DIR:=/var/lib/$BRAND_SHORT}"
  : "${REPORT_DIR:=$STATE_DIR/reports}"
  : "${LOCK_DIR:=/run/lock}"
  : "${RSYNC_BWLIMIT:=10000}"
  : "${DUMP_MAX_AGE_H:=26}"
  : "${SNAP_PREFIX_SYNC:=$BRAND_SHORT}"
  : "${SANOID_PREFIX:=autosnap_}"
  : "${RESTIC_ENV_FILE:=}"
  : "${RESTIC_SETS_FILE:=}"
  : "${RESTIC_LIMIT_UPLOAD:=8000}"
  : "${RESTIC_KEEP_DAILY:=7}"
  : "${RESTIC_KEEP_WEEKLY:=4}"
  : "${RESTIC_KEEP_MONTHLY:=6}"
  : "${RESTIC_CACHE_DIR:=/var/cache/restic}"
  : "${RESTIC_SRC_MNT:=/run/$BRAND_SHORT/src}"
  : "${RESTIC_HOST:=$(hostname -s)}"
  : "${RESTORE_TEST_APP:=}"
  : "${RESTORE_TEST_SAMPLE_TAG:=}"
  : "${RESTORE_TEST_SAMPLES:=10}"
  : "${MAX_AGE_SANOID_MIN:=120}"
  : "${MAX_AGE_SKBAK_H:=26}"
  : "${MAX_AGE_RESTIC_H:=36}"
  : "${RESTIC_SEED_GRACE_D:=7}"
  : "${CHECK_RESTIC_REPO:=0}"
  : "${POOL_CAP_WARN:=80}"
  : "${THINPOOL:=}"
  : "${THINPOOL_WARN:=85}"
  : "${PREDEPLOY_KEEP_DAYS:=30}"
  : "${PREDEPLOY_KEEP_MIN:=2}"
  : "${NOTIFY_MODE:=log}"
  : "${NOTIFY_LOG:=$STATE_DIR/alerts.log}"
  : "${NOTIFY_COMMAND:=}"
  export RESTIC_CACHE_DIR
}

# Mountpoint of a dataset (live filesystem).
skb_mountpoint() { zfs get -H -o value mountpoint "$1"; }

# apps.conf line: NAME SRC_RELPATH KEEP_DAILY KEEP_WEEKLY KEEP_MONTHLY [OPT...]
# OPT is one of:
#   --exclude=PATTERN   rsync pattern relative to the app source dir
#                       (leading "/" anchors it to the app root)
#   newest=DIR:N        keep only the newest N files of DIR (dump dirs)
#   dump=DIR            DIR must hold a file newer than DUMP_MAX_AGE_H
# Prints the parsed line as tab-separated fields; returns 1 for blank/comment.
skb_parse_app_line() {
  local line="$1"
  line="${line%%#*}"
  # shellcheck disable=SC2086
  set -- $line
  [ $# -ge 5 ] || return 1
  local name="$1" src="$2" kd="$3" kw="$4" km="$5"
  shift 5
  case "$name" in *[!A-Za-z0-9._-]*|'') return 2;; esac
  case "$src" in /*|*..*) return 2;; esac
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$src" "$kd" "$kw" "$km" "$*"
}

# Emit rsync --exclude args (one per line) for "newest=DIR:N" options,
# excluding every regular file in ROOT/DIR except the newest N (by mtime).
skb_newest_excludes() {
  local root="$1"; shift
  local opt dir n f
  for opt in "$@"; do
    case "$opt" in
      newest=*:*)
        dir="${opt#newest=}"; n="${dir##*:}"; dir="${dir%:*}"
        [ -d "$root/$dir" ] || continue
        find "$root/$dir" -maxdepth 1 -type f -printf '%T@\t%f\n' \
          | sort -rn | tail -n +"$((n + 1))" | cut -f2- \
          | while IFS= read -r f; do printf -- '--exclude=/%s/%s\n' "$dir" "$f"; done
        ;;
    esac
  done
}

# Convert rsync-style excludes of one app into restic --exclude args.
# $1 = absolute source dir of the app; rest = options from apps.conf.
skb_restic_excludes() {
  local base="$1"; shift
  local opt pat
  for opt in "$@"; do
    case "$opt" in
      --exclude=*)
        pat="${opt#--exclude=}"; pat="${pat%/}"
        case "$pat" in
          /*) printf -- '--exclude=%s%s\n' "$base" "$pat" ;;
          *)  printf -- '--exclude=%s\n' "$pat" ;;
        esac
        ;;
    esac
  done
}

# Given snapshot names (one per line, oldest first) print the ones to destroy
# so that only the newest KEEP remain. KEEP < 1 is treated as 1.
skb_prune_list() {
  local keep="$1"
  [ "$keep" -ge 1 ] 2>/dev/null || keep=1
  head -n -"$keep"
}

# Newest snapshot of DATASET whose short name starts with PREFIX.
skb_newest_snapshot() {
  local ds="$1" prefix="$2"
  zfs list -H -p -t snapshot -o name -s creation -d 1 "$ds" \
    | awk -F@ -v p="$prefix" 'index($2, p) == 1' | tail -n 1
}

# Epoch creation time of a snapshot.
skb_snap_epoch() { zfs get -H -p -o value creation "$1"; }

# Age in whole hours of an epoch timestamp.
skb_age_h() { echo $(( ( $(date +%s) - $1 ) / 3600 )); }

# Load the restic environment (RESTIC_ENV_FILE) into the process. B2 keys
# double as S3 keys for the s3: backend.
skb_restic_env() {
  [ -r "$RESTIC_ENV_FILE" ] || skb_die "restic env file not readable: $RESTIC_ENV_FILE"
  set -a
  # shellcheck disable=SC1090
  . "$RESTIC_ENV_FILE"
  set +a
  : "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY missing in $RESTIC_ENV_FILE}"
  : "${RESTIC_PASSWORD:?RESTIC_PASSWORD missing in $RESTIC_ENV_FILE}"
  case "$RESTIC_REPOSITORY" in
    s3:*)
      export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-${B2_ACCOUNT_ID:-}}"
      export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-${B2_ACCOUNT_KEY:-}}"
      if [ -n "${B2_REGION:-}" ]; then export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$B2_REGION}"; fi
      ;;
  esac
}

# --- brand, notifications, reports -----------------------------------------

skb_banner() {
  printf '%s\n' "$BRAND_PRODUCT"
  [ -z "$BRAND_TAGLINE" ] || printf '%s\n' "$BRAND_TAGLINE"
  [ -z "$BRAND_CREDITS_TEXT" ] || printf '%s\n' "$BRAND_CREDITS_TEXT"
  return 0
}

skb_footer() {
  printf -- '-- \n'
  [ -z "$BRAND_FOOTER" ] || printf '%s\n' "$BRAND_FOOTER"
  [ -z "$BRAND_CONTACT" ] || printf 'Contact: %s\n' "$BRAND_CONTACT"
  [ -z "$BRAND_CREDITS_TEXT" ] || printf '%s\n' "$BRAND_CREDITS_TEXT"
  return 0
}

# skb_notify LEVEL KEY SUBJECT BODY -- LEVEL is info, warn or crit. Always
# appended to NOTIFY_LOG; with NOTIFY_MODE=command also handed to
# NOTIFY_COMMAND: one line split on whitespace (never a shell), where every
# word has {level} {key} {subject} {message} {body} substituted.
SKB_NOTIFY_FAILED=0
skb_notify() {
  local level=$1 key=$2 subject=$3 body=$4 message full w
  [ "$NOTIFY_MODE" != none ] || return 0
  message="[$BRAND_ALERT_PREFIX] $level: $subject"
  full="$body"$'\n\n'"$(skb_footer)"
  mkdir -p "$(dirname "$NOTIFY_LOG")"
  {
    printf '%s %s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$level" "$key" "$message"
    printf '%s\n' "$full" | sed 's/^/    /'
  } >>"$NOTIFY_LOG"
  [ "$NOTIFY_MODE" = command ] || return 0
  local words=() args=()
  read -r -a words <<<"$NOTIFY_COMMAND"
  for w in "${words[@]}"; do
    w=${w//\{level\}/$level}
    w=${w//\{key\}/$key}
    w=${w//\{subject\}/$subject}
    w=${w//\{body\}/$full}
    w=${w//\{message\}/$message}
    args+=("$w")
  done
  if ! BK_LEVEL=$level BK_KEY=$key BK_SUBJECT=$message BK_BODY=$full \
      BK_BRAND_PRODUCT=$BRAND_PRODUCT BK_BRAND_LOGO_URL=$BRAND_LOGO_URL BK_BRAND_CONTACT=$BRAND_CONTACT \
      "${args[@]}"; then
    skb_log "notifier failed for $key" >&2
    # shellcheck disable=SC2034  # read by check
    SKB_NOTIFY_FAILED=1
  fi
}

# skb_report NAME TITLE < body -> REPORT_DIR/NAME with the brand header and footer; prints the path.
skb_report() {
  local name=$1 title=$2
  mkdir -p "$REPORT_DIR"
  {
    printf '%s: %s\n' "$BRAND_PRODUCT" "$title"
    printf 'host: %s   time: %s\n' "$RESTIC_HOST" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    [ -z "$BRAND_LOGO_URL" ] || printf 'logo: %s\n' "$BRAND_LOGO_URL"
    printf '\n'
    cat
    printf '\n'
    skb_footer
  } >"$REPORT_DIR/$name"
  printf '%s\n' "$REPORT_DIR/$name"
}
