# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Local tiers:
#   1  snapshots of the whole data target (builtin engine) and the
#      pre-deploy snapshot helper (sanoid owns tier 1 retention on ZFS);
#   2  per-app copy from a frozen snapshot into a second pool or disk,
#      with daily/weekly/monthly history snapshots per app;
#   3  DB dump hooks and dump freshness gates, run before the snapshot so
#      the dumps are inside it.

# --- tier 1 -----------------------------------------------------------------

cmd_snapshot() {
  if [ "$SNAPSHOTS_ENABLED" != 1 ]; then log "snapshots tier disabled"; return 0; fi
  if [ "$SNAPSHOTS_ENGINE" != builtin ]; then
    log "snapshots are taken by $SNAPSHOTS_ENGINE on this host; nothing to do"
    return 0
  fi
  take_lock
  local name
  name="$SNAP_PREFIX-auto-$(ts)"
  snap_create "$DATA_TARGET" "$name"
  printf 'snapshot: %s@%s\n' "$DATA_TARGET" "$name"
  prune_keep "$DATA_TARGET" "$SNAP_PREFIX-auto-" "$SNAPSHOTS_KEEP"
  stamp snapshot
}

cmd_predeploy() {  # predeploy PURPOSE -- no lock: a snapshot is safe beside a running copy
  local purpose=${1:-}
  [[ $purpose =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
    || usage_die "predeploy PURPOSE: letters, digits, '.', '_' and '-' only (got '${purpose}')"
  local name n names=() i cutoff
  name="pre-$purpose-$(ts)"
  snap_create "$DATA_TARGET" "$name"
  printf 'snapshot: %s@%s\n' "$DATA_TARGET" "$name"
  # prune pre-* snapshots older than PREDEPLOY_KEEP_DAYS, always keeping the newest PREDEPLOY_KEEP_MIN
  while IFS= read -r n; do
    [[ $n == pre-* ]] && names+=("$n")
  done < <(snap_list "$DATA_TARGET")
  cutoff=$(( $(now) - PREDEPLOY_KEEP_DAYS * 86400 ))
  for (( i = 0; i < ${#names[@]} - PREDEPLOY_KEEP_MIN; i++ )); do
    if [ "$(snap_created "$DATA_TARGET" "${names[$i]}")" -lt "$cutoff" ]; then
      snap_destroy "$DATA_TARGET" "${names[$i]}"
      log "pruned $DATA_TARGET@${names[$i]} (older than $PREDEPLOY_KEEP_DAYS days)"
    fi
  done
}

# --- tier 3 -----------------------------------------------------------------

run_dump_hooks() {  # -> number of failed hooks in HOOK_FAILURES
  HOOK_FAILURES=0
  local rec name tmo cmd rc
  for rec in "${DUMP_HOOKS[@]}"; do
    IFS='|' read -r name tmo cmd <<<"$rec"
    log "dump hook $name (timeout ${tmo}s)"
    rc=0
    timeout "$tmo" bash -c "$cmd" || rc=$?
    if [ "$rc" != 0 ]; then
      notify crit "$BRAND_SHORT-hook-$name" "dump hook $name failed on $HOST_LABEL" \
        "The dump hook '$name' exited $rc (124 = timed out after ${tmo}s). Its dump may be missing or partial."
      HOOK_FAILURES=$((HOOK_FAILURES + 1))
    fi
  done
}

dump_problem() {  # dump_problem APP GLOB MAX_H -> prints a problem, or nothing if the newest dump is fine
  local app=$1 glob=$2 maxh=$3 base f newest="" newest_t=0 t size age
  base="$(target_path "$DATA_TARGET")/$app"
  # the glob is expanded on purpose (e.g. dump/*.sql.gz)
  # shellcheck disable=SC2206
  local files=( "$base"/$glob )
  for f in "${files[@]}"; do
    [ -f "$f" ] || continue
    t=$(stat -c %Y "$f")
    if [ "$t" -ge "$newest_t" ]; then newest=$f; newest_t=$t; fi
  done
  if [ -z "$newest" ]; then printf 'no dump matches %s/%s\n' "$app" "$glob"; return; fi
  size=$(stat -c %s "$newest")
  age=$(( $(now) - newest_t ))
  if [ "$size" = 0 ]; then printf 'newest dump %s is empty (0 bytes)\n' "${newest#"$base"/}"; return; fi
  if [ "$age" -gt $(( maxh * 3600 )) ]; then
    printf 'newest dump %s is stale: %sh old (max %sh)\n' "${newest#"$base"/}" "$(hours "$age")" "$maxh"
  fi
}

run_dump_checks() {  # -> DUMP_FAILURES
  DUMP_FAILURES=0
  local rec app glob maxh problem
  for rec in "${DUMP_CHECKS[@]}"; do
    IFS='|' read -r app glob maxh <<<"$rec"
    problem=$(dump_problem "$app" "$glob" "${maxh:-$MAX_AGE_DUMP_H}")
    if [ -n "$problem" ]; then
      notify crit "$BRAND_SHORT-dump-$app" "dump $app: $problem" \
        "The dump check for $app on $HOST_LABEL failed: $problem. A snapshot taken now would not hold a current dump."
      DUMP_FAILURES=$((DUMP_FAILURES + 1))
    fi
  done
}

# --- tier 2 -----------------------------------------------------------------

history_snapshot() {  # history_snapshot TARGET KEEP_DAILY KEEP_WEEKLY KEEP_MONTHLY
  local target=$1 kd=$2 kw=$3 km=$4 t epoch
  epoch=$(now)
  t=$(ts "$epoch")
  [ "$kd" -ge 1 ] 2>/dev/null || kd=1
  snap_create "$target" "$SNAP_PREFIX-daily-$t"
  if [ "$kw" -gt 0 ] && [ "$(date -u -d "@$epoch" +%u)" = 7 ]; then snap_create "$target" "$SNAP_PREFIX-weekly-$t"; fi
  if [ "$km" -gt 0 ] && [ "$(date -u -d "@$epoch" +%d)" = 01 ]; then snap_create "$target" "$SNAP_PREFIX-monthly-$t"; fi
  prune_keep "$target" "$SNAP_PREFIX-daily-" "$kd"
  prune_keep "$target" "$SNAP_PREFIX-weekly-" "$kw"
  prune_keep "$target" "$SNAP_PREFIX-monthly-" "$km"
}

copy_apps() {  # copy_apps SNAPSHOT -> COPY_FAILURES; SNAPSHOT is a snapshot of DATA_TARGET
  COPY_FAILURES=0
  local snap=$1 src_root rec app dst dpath e args
  src_root=$(snap_path "$DATA_TARGET" "$snap")
  for rec in "${COPY_APPS[@]}"; do
    record "$rec"
    app=${REC_FIELDS[0]}
    dst="$COPY_TARGET/$app"
    if [ ! -d "$src_root/$app" ]; then
      notify crit "$BRAND_SHORT-copy-$app" "copy $app: not in the data snapshot" \
        "$app has no directory in $DATA_TARGET@$snap on $HOST_LABEL; nothing was copied."
      COPY_FAILURES=$((COPY_FAILURES + 1))
      continue
    fi
    target_ensure "$dst"
    dpath=$(target_path "$dst")
    # shellcheck disable=SC2206  # COPY_RSYNC_FLAGS is a word list on purpose
    args=( ${COPY_RSYNC_FLAGS:--aHAX --numeric-ids} --delete --delete-excluded )
    [ "$COPY_BWLIMIT_KBPS" -gt 0 ] && args+=("--bwlimit=$COPY_BWLIMIT_KBPS")
    for e in "${REC_FIELDS[@]:4}"; do [ -z "$e" ] || args+=("--exclude=$e"); done
    log "copy $app: $src_root/$app/ -> $dpath/"
    if nicely rsync "${args[@]}" "$src_root/$app/" "$dpath/"; then
      history_snapshot "$dst" "${REC_FIELDS[1]:-7}" "${REC_FIELDS[2]:-4}" "${REC_FIELDS[3]:-3}"
      stamp "copy-$app"
    else
      notify crit "$BRAND_SHORT-copy-$app" "copy $app failed on $HOST_LABEL" \
        "rsync from $DATA_TARGET@$snap to $dst failed; the previous copy and its history are unchanged."
      COPY_FAILURES=$((COPY_FAILURES + 1))
    fi
  done
}

# The transient snapshot a run copies from; destroyed when the run ends.
RUN_SNAP=""
run_snapshot_begin() {
  RUN_SNAP="$SNAP_PREFIX-run-$(ts)"
  snap_create "$DATA_TARGET" "$RUN_SNAP"
  trap run_snapshot_end EXIT
  log "frozen: $DATA_TARGET@$RUN_SNAP"
}
run_snapshot_end() {
  if [ -n "$RUN_SNAP" ]; then
    snap_destroy "$DATA_TARGET" "$RUN_SNAP" || warn "could not destroy $DATA_TARGET@$RUN_SNAP"
    RUN_SNAP=""
  fi
}

cmd_copy() {
  [ "$COPY_ENABLED" = 1 ] || { log "copy tier disabled"; return 0; }
  take_lock
  run_snapshot_begin
  copy_apps "$RUN_SNAP"
  run_snapshot_end
  [ "$COPY_FAILURES" = 0 ]
}
