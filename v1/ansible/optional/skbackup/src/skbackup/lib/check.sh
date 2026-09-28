# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Tier 6: staleness and capacity checks, and the status overview.
#
# check reads the ARTIFACTS, not the engine's own success stamps: the newest
# snapshot on the data target, the newest history snapshot per copied app,
# the newest dump file, and the newest restic snapshot per set/app as the
# repository itself reports it. A run that "succeeded" but left nothing
# behind is still stale. Exit 0 = all fresh, 1 = warnings, 2 = critical.

CHECK_LINES=()
CHECK_CRIT=0
CHECK_WARN=0

finding() {  # finding LEVEL KEY SUBJECT BODY
  notify "$1" "$2" "$3" "$4"
  CHECK_LINES+=("$1 $3")
  if [ "$1" = crit ]; then CHECK_CRIT=$((CHECK_CRIT + 1)); else CHECK_WARN=$((CHECK_WARN + 1)); fi
}
ok() { CHECK_LINES+=("ok   $*"); }

newest_snapshot() {  # newest_snapshot TARGET PREFIX_REGEX -> name, or nothing
  snap_list "$1" | grep -E "^($2)" | tail -n 1 || true
}

check_age() {  # check_age KEY LABEL TARGET NAME MAX_H
  local key=$1 label=$2 target=$3 name=$4 maxh=$5 age
  if [ -z "$name" ]; then
    finding crit "$key" "$label: none found" "No snapshot for $label exists on $HOST_LABEL ($target)."
    return
  fi
  age=$(( $(now) - $(snap_created "$target" "$name") ))
  if [ "$age" -gt $(( maxh * 3600 )) ]; then
    finding crit "$key" "$label: newest is $(hours "$age")h old (max ${maxh}h)" \
      "The newest snapshot for $label on $HOST_LABEL is $target@$name, $(hours "$age") hours old. Expected at most $maxh hours."
  else
    ok "$label: $name ($(hours "$age")h old)"
  fi
}

cmd_check() {
  local rec app set pattern latest age problem pct target
  if [ "$SNAPSHOTS_ENABLED" = 1 ]; then
    if [ "$SNAPSHOTS_ENGINE" = sanoid ]; then pattern="autosnap_"; else pattern="$SNAP_PREFIX-auto-"; fi
    check_age "$BRAND_SHORT-snapshot" "snapshot" "$DATA_TARGET" \
      "$(newest_snapshot "$DATA_TARGET" "$pattern")" "$MAX_AGE_SNAPSHOT_H"
  fi
  if [ "$COPY_ENABLED" = 1 ]; then
    for rec in "${COPY_APPS[@]}"; do
      record "$rec"
      app=${REC_FIELDS[0]}
      check_age "$BRAND_SHORT-copy-$app" "copy $app" "$COPY_TARGET/$app" \
        "$(newest_snapshot "$COPY_TARGET/$app" "$SNAP_PREFIX-(daily|weekly|monthly)-")" "$MAX_AGE_COPY_H"
    done
  fi
  if [ "$DUMPS_ENABLED" = 1 ]; then
    local glob maxh
    for rec in "${DUMP_CHECKS[@]}"; do
      IFS='|' read -r app glob maxh <<<"$rec"
      problem=$(dump_problem "$app" "$glob" "${maxh:-$MAX_AGE_DUMP_H}")
      if [ -n "$problem" ]; then
        finding crit "$BRAND_SHORT-dump-$app" "dump $app: $problem" "The dump check for $app on $HOST_LABEL failed: $problem."
      else
        ok "dump $app: fresh"
      fi
    done
  fi
  if [ "$OFFSITE_ENABLED" = 1 ]; then
    if ! ( offsite_env ) 2>/dev/null; then
      finding crit "$BRAND_SHORT-offsite" "offsite: restic environment unreadable" "$OFFSITE_ENV_FILE cannot be read on $HOST_LABEL."
    else
      offsite_env
      for rec in "${OFFSITE_SETS[@]}"; do
        record "$rec"
        set=${REC_FIELDS[0]}
        split_commas "${REC_FIELDS[2]:-}"
        for app in "${COMMA_FIELDS[@]}"; do
          if ! latest=$(offsite_latest "$set" "$app" 2>/dev/null); then
            finding crit "$BRAND_SHORT-offsite-$set-$app" "offsite $set/$app: repository unreachable" \
              "restic could not list snapshots for $set/$app from $HOST_LABEL."
          elif [ -z "$latest" ]; then
            finding crit "$BRAND_SHORT-offsite-$set-$app" "offsite $set/$app: no snapshot" \
              "The repository holds no snapshot for $set/$app from $HOST_LABEL."
          else
            read -r _ age _ <<<"$latest"
            age=$(( $(now) - age ))
            if [ "$age" -gt $(( MAX_AGE_OFFSITE_H * 3600 )) ]; then
              finding crit "$BRAND_SHORT-offsite-$set-$app" "offsite $set/$app: newest is $(hours "$age")h old (max ${MAX_AGE_OFFSITE_H}h)" \
                "The newest offsite snapshot for $set/$app from $HOST_LABEL is $(hours "$age") hours old."
            else
              ok "offsite $set/$app: $(hours "$age")h old"
            fi
          fi
        done
      done
    fi
  fi
  if [ "$RESTORE_TEST_ENABLED" = 1 ]; then
    age=$(stamp_age restore-test)
    if [ -z "$age" ] || [ "$age" -gt $(( MAX_AGE_RESTORE_TEST_H * 3600 )) ]; then
      finding warn "$BRAND_SHORT-restore-test-age" "restore test: no pass in ${MAX_AGE_RESTORE_TEST_H}h" \
        "The last passing restore test on $HOST_LABEL is older than $MAX_AGE_RESTORE_TEST_H hours (or never ran)."
    else
      ok "restore test: passed $(hours "$age")h ago"
    fi
  fi
  local targets=("$DATA_TARGET")
  [ "$COPY_ENABLED" != 1 ] || targets+=("$COPY_TARGET")
  for target in "${targets[@]}"; do
    pct=$(capacity_pct "$target" 2>/dev/null || true)
    [ -n "$pct" ] || continue
    if [ "$pct" -ge "$POOL_CAP_WARN_PCT" ]; then
      finding warn "$BRAND_SHORT-capacity" "capacity: $target at $pct% (warn at $POOL_CAP_WARN_PCT%)" \
        "The pool or filesystem holding $target on $HOST_LABEL is $pct% full."
    else
      ok "capacity: $target at $pct%"
    fi
  done
  local summary="all fresh"
  [ "$CHECK_WARN" = 0 ] || summary="$CHECK_WARN warning(s)"
  [ "$CHECK_CRIT" = 0 ] || summary="$CHECK_CRIT critical, $CHECK_WARN warning(s)"
  CHECK_LINES+=("" "RESULT: $summary")
  printf '%s\n' "${CHECK_LINES[@]}" | report_write check-latest.txt "backup check" >/dev/null
  printf '%s\n' "${CHECK_LINES[@]}"
  [ "$NOTIFY_FAILED" = 0 ] || warn "one or more notifications were not delivered"
  if [ "$CHECK_CRIT" != 0 ]; then return 2; fi
  if [ "$CHECK_WARN" != 0 ]; then return 1; fi
  return 0
}

tier_line() {  # tier_line N NAME ENABLED DETAIL
  local state=off
  [ "$3" = 1 ] && state=on
  printf '  tier %s  %-13s %-3s %s\n' "$1" "$2" "$state" "$4"
}

stamp_text() {
  local a
  a=$(stamp_age "$1")
  if [ -z "$a" ]; then printf 'never'; else printf '%sh ago' "$(hours "$a")"; fi
}

cmd_status() {
  banner
  printf '\nhost %s, %s backend, data %s\n\n' "$HOST_LABEL" "$SNAP_BACKEND" "$DATA_TARGET"
  local newest
  newest=$(snap_list "$DATA_TARGET" 2>/dev/null | tail -n 1 || true)
  tier_line 1 snapshots "$SNAPSHOTS_ENABLED" "engine $SNAPSHOTS_ENGINE, newest ${newest:-none}"
  tier_line 2 copy "$COPY_ENABLED" "target ${COPY_TARGET:-none}, ${#COPY_APPS[@]} app(s)"
  local rec
  if [ "$COPY_ENABLED" = 1 ]; then
    for rec in "${COPY_APPS[@]}"; do
      record "$rec"
      printf '            %-24s last copy %s\n' "${REC_FIELDS[0]}" "$(stamp_text "copy-${REC_FIELDS[0]}")"
    done
  fi
  tier_line 3 dumps "$DUMPS_ENABLED" "${#DUMP_HOOKS[@]} hook(s), ${#DUMP_CHECKS[@]} check(s)"
  tier_line 4 offsite "$OFFSITE_ENABLED" "${#OFFSITE_SETS[@]} set(s)"
  if [ "$OFFSITE_ENABLED" = 1 ]; then
    for rec in "${OFFSITE_SETS[@]}"; do
      record "$rec"
      split_commas "${REC_FIELDS[2]:-}"
      local app
      for app in "${COMMA_FIELDS[@]}"; do
        printf '            %-24s last upload %s\n' "${REC_FIELDS[0]}/$app" "$(stamp_text "offsite-${REC_FIELDS[0]}-$app")"
      done
    done
  fi
  tier_line 5 restore-test "$RESTORE_TEST_ENABLED" "last pass $(stamp_text restore-test)"
  tier_line 6 alerts "$ALERTS_ENABLED" "notifier $NOTIFY_MODE, log $NOTIFY_LOG"
  printf '\nlast run: %s\n' "$(stamp_text run)"
  if [ -r "$REPORT_DIR/check-latest.txt" ]; then
    printf 'last check: %s\n' "$(grep '^RESULT:' "$REPORT_DIR/check-latest.txt" | cut -d' ' -f2-)"
  fi
  printf '\n'
  footer
}
