# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Core helpers: configuration, time, logging, locking, state stamps, the
# brand (banner, footer) and notifications.
#
# Every product name, alert prefix, footer and contact line this engine
# prints comes from the config file's BRAND_* variables. The engine itself
# carries no brand string, so a white-labelled instance shows only its own.

# Variables every config must define (the deploy renders all of them).
BK_REQUIRED_VARS=(
  BRAND_PRODUCT BRAND_SHORT BRAND_TAGLINE BRAND_ALERT_PREFIX BRAND_FOOTER BRAND_CONTACT
  BRAND_LOGO_URL BRAND_CREDITS_TEXT UNIT_BASE HOST_LABEL STATE_DIR LOCK_FILE REPORT_DIR
  SNAP_BACKEND SNAP_DIR_ROOT SNAP_PREFIX DATA_TARGET IONICE
  SNAPSHOTS_ENABLED SNAPSHOTS_ENGINE SNAPSHOTS_KEEP PREDEPLOY_KEEP_DAYS PREDEPLOY_KEEP_MIN
  COPY_ENABLED COPY_TARGET COPY_BWLIMIT_KBPS
  DUMPS_ENABLED DUMPS_REQUIRED
  OFFSITE_ENABLED OFFSITE_ENV_FILE OFFSITE_LIMIT_UPLOAD_KBPS OFFSITE_IGNORE_INODE
  RESTORE_TEST_ENABLED RESTORE_TEST_SAMPLES RESTORE_TEST_READ_SUBSET
  ALERTS_ENABLED NOTIFY_MODE NOTIFY_LOG NOTIFY_COMMAND
  MAX_AGE_SNAPSHOT_H MAX_AGE_COPY_H MAX_AGE_DUMP_H MAX_AGE_OFFSITE_H MAX_AGE_RESTORE_TEST_H
  POOL_CAP_WARN_PCT
)
# Record arrays (may be empty, must exist).
BK_REQUIRED_ARRAYS=(COPY_APPS DUMP_HOOKS DUMP_CHECKS OFFSITE_TAGS OFFSITE_SETS OFFSITE_FORGET_ARGS RESTORE_TEST_HOOKS)

# bash 5.2 would expand "&" in a ${var//pattern/replacement} replacement.
shopt -u patsub_replacement 2>/dev/null || true

load_config() {  # load_config FILE
  local file=$1 v missing=()
  if [ -z "$file" ] || [ ! -r "$file" ]; then
    printf 'config file not found or not readable: %s\n' "${file:-<none>}" >&2
    exit 2
  fi
  # shellcheck source=/dev/null
  . "$file"
  for v in "${BK_REQUIRED_VARS[@]}"; do
    [ -n "${!v+x}" ] || missing+=("$v")
  done
  for v in "${BK_REQUIRED_ARRAYS[@]}"; do
    declare -p "$v" >/dev/null 2>&1 || missing+=("$v")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    printf 'config %s is missing: %s\n' "$file" "${missing[*]}" >&2
    exit 2
  fi
  case "$SNAP_BACKEND" in zfs | dir) : ;; *) printf 'config: SNAP_BACKEND must be zfs or dir\n' >&2; exit 2 ;; esac
}

now() {  # epoch seconds; BK_NOW overrides (tests, and "what would check say at time T")
  if [ -n "${BK_NOW:-}" ]; then printf '%s\n' "$BK_NOW"; else date -u +%s; fi
}
ts() { date -u -d "@${1:-$(now)}" +%Y%m%dT%H%M%SZ; }
iso() { date -u -d "@${1:-$(now)}" +%Y-%m-%dT%H:%M:%SZ; }
hours() { printf '%s' $(( ($1 + 1800) / 3600 )); }

log() { printf '%s %s\n' "$(iso)" "$*"; }
warn() { printf '%s: %s\n' "$BRAND_SHORT" "$*" >&2; }
die() { warn "$*"; exit 1; }
usage_die() { warn "$*"; exit 2; }

nicely() {  # run a heavy command at idle I/O and CPU priority when configured
  if [ "$IONICE" = 1 ]; then ionice -c3 nice -n19 "$@"; else "$@"; fi
}

take_lock() {  # one run at a time (run, copy, offsite, prune, restore-test, snapshot)
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    warn "another run holds the lock ($LOCK_FILE); not starting"
    exit 75
  fi
}

stamp() {  # stamp NAME -> STATE_DIR/last-ok-NAME holds the epoch of the last success
  mkdir -p "$STATE_DIR"
  now >"$STATE_DIR/last-ok-$1"
}
stamp_age() {  # stamp_age NAME -> seconds since the stamp, or empty if never
  local f="$STATE_DIR/last-ok-$1"
  [ -r "$f" ] || return 0
  printf '%s\n' $(( $(now) - $(cat "$f") ))
}

record() {  # record REC -> split a "a|b|c" config record into the array REC_FIELDS
  local IFS='|'
  # shellcheck disable=SC2034  # REC_FIELDS is read by the caller
  read -r -a REC_FIELDS <<<"$1"
}
split_commas() {  # split_commas STR -> COMMA_FIELDS
  local IFS=','
  # shellcheck disable=SC2034
  read -r -a COMMA_FIELDS <<<"$1"
}

banner() {
  printf '%s %s\n' "$BRAND_PRODUCT" "$ENGINE_VERSION"
  [ -z "$BRAND_TAGLINE" ] || printf '%s\n' "$BRAND_TAGLINE"
  [ -z "$BRAND_CREDITS_TEXT" ] || printf '%s\n' "$BRAND_CREDITS_TEXT"
  return 0
}

footer() {
  printf -- '-- \n%s\n' "$BRAND_FOOTER"
  [ -z "$BRAND_CONTACT" ] || printf 'Contact: %s\n' "$BRAND_CONTACT"
  [ -z "$BRAND_CREDITS_TEXT" ] || printf '%s\n' "$BRAND_CREDITS_TEXT"
  return 0
}

NOTIFY_FAILED=0
notify() {  # notify LEVEL KEY SUBJECT BODY -- LEVEL is info, warn or crit
  local level=$1 key=$2 subject=$3 body=$4 message full w
  message="[$BRAND_ALERT_PREFIX] $level: $subject"
  full="$body"$'\n\n'"$(footer)"
  mkdir -p "$(dirname "$NOTIFY_LOG")"
  {
    printf '%s %s %s %s\n' "$(iso)" "$level" "$key" "$message"
    printf '%s\n' "$full" | sed 's/^/    /'
  } >>"$NOTIFY_LOG"
  [ "$ALERTS_ENABLED" = 1 ] || return 0
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
    warn "notifier failed for $key ($message)"
    # shellcheck disable=SC2034  # read by cmd_check
    NOTIFY_FAILED=1
  fi
}

report_write() {  # report_write NAME TITLE < body -> REPORT_DIR/NAME, with brand header and footer
  local name=$1 title=$2
  mkdir -p "$REPORT_DIR"
  {
    printf '%s: %s\n' "$BRAND_PRODUCT" "$title"
    printf 'host: %s   time: %s\n' "$HOST_LABEL" "$(iso)"
    [ -z "$BRAND_LOGO_URL" ] || printf 'logo: %s\n' "$BRAND_LOGO_URL"
    printf '\n'
    cat
    printf '\n'
    footer
  } >"$REPORT_DIR/$name"
  printf '%s\n' "$REPORT_DIR/$name"
}
