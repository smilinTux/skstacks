# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Tier 4 (restic offsite), tier 5 (restore test) and the restore commands.
#
# One restic snapshot per (set, app), always taken from a frozen snapshot:
# a set with source=copy reads the app's newest tier 2 history snapshot, a
# set with source=data reads the run's transient snapshot of the data
# target. restic runs inside that directory ("backup ."), so every
# snapshot's tree starts at the app's root whatever the frozen path was,
# and the previous snapshot of the same set/app is passed as --parent.
# Tags: OFFSITE_TAGS, the set's own tags, set:<set>, app:<app>, src:<frozen
# snapshot name>. --host is HOST_LABEL.

rs() { "${RESTIC_BIN:-restic}" "$@"; }

offsite_env() {
  [ -r "$OFFSITE_ENV_FILE" ] || die "restic environment $OFFSITE_ENV_FILE not readable"
  set -a
  # shellcheck source=/dev/null
  . "$OFFSITE_ENV_FILE"
  set +a
}

offsite_latest() {  # offsite_latest SET APP -> "<id> <epoch> <src-name>" of the newest snapshot, or nothing
  local json id time src
  json=$(rs snapshots --json --no-lock --host "$HOST_LABEL" --tag "set:$1,app:$2") || return 1
  read -r id time src < <(printf '%s' "$json" | jq -r 'sort_by(.time) | last // empty
    | [.id, .time, ((.tags // []) | map(select(startswith("src:"))) | first // "src:-" | ltrimstr("src:"))]
    | @tsv') || true
  [ -n "${id:-}" ] || return 0
  printf '%s %s %s\n' "$id" "$(date -u -d "$time" +%s)" "$src"
}

set_source() {  # set_source SOURCE APP -> "<frozen dir> <frozen name>" for a set's app, or nothing
  local source=$1 app=$2 name
  case "$source" in
    copy)
      name=$(snap_list "$COPY_TARGET/$app" | grep -E "^$SNAP_PREFIX-(daily|weekly|monthly)-" | tail -n 1 || true)
      [ -n "$name" ] && printf '%s %s\n' "$(snap_path "$COPY_TARGET/$app" "$name")" "$name"
      ;;
    data)
      [ -n "$RUN_SNAP" ] && printf '%s/%s %s\n' "$(snap_path "$DATA_TARGET" "$RUN_SNAP")" "$app" "$RUN_SNAP"
      ;;
  esac
  return 0
}

sets_need_data_snapshot() {
  local rec
  for rec in "${OFFSITE_SETS[@]}"; do
    record "$rec"
    [ "${REC_FIELDS[1]}" = data ] && return 0
  done
  return 1
}

offsite_sets() {  # -> OFFSITE_FAILURES
  OFFSITE_FAILURES=0
  offsite_env
  local rec set source app t e src name parent args latest
  for rec in "${OFFSITE_SETS[@]}"; do
    record "$rec"
    set=${REC_FIELDS[0]} source=${REC_FIELDS[1]}
    split_commas "${REC_FIELDS[2]:-}"
    local apps=("${COMMA_FIELDS[@]}")
    split_commas "${REC_FIELDS[3]:-}"
    local set_tags=("${COMMA_FIELDS[@]}")
    split_commas "${REC_FIELDS[4]:-}"
    local excludes=("${COMMA_FIELDS[@]}")
    for app in "${apps[@]}"; do
      read -r src name < <(set_source "$source" "$app") || true
      if [ -z "${src:-}" ] || [ ! -d "$src" ]; then
        notify crit "$BRAND_SHORT-offsite-$set-$app" "offsite $set/$app: no frozen source" \
          "No $source snapshot of $app exists on $HOST_LABEL to back up; the offsite copy was not updated."
        OFFSITE_FAILURES=$((OFFSITE_FAILURES + 1))
        src="" name=""
        continue
      fi
      parent=""
      latest=$(offsite_latest "$set" "$app" || true)
      [ -z "$latest" ] || parent=${latest%% *}
      args=(backup . --host "$HOST_LABEL" --tag "set:$set" --tag "app:$app" --tag "src:$name")
      for t in "${OFFSITE_TAGS[@]}" "${set_tags[@]}"; do [ -z "$t" ] || args+=(--tag "$t"); done
      for e in "${excludes[@]}"; do [ -z "$e" ] || args+=(--exclude "$e"); done
      [ -z "$parent" ] || args+=(--parent "$parent")
      [ "$OFFSITE_IGNORE_INODE" != 1 ] || args+=(--ignore-inode)
      [ "$OFFSITE_LIMIT_UPLOAD_KBPS" = 0 ] || args+=(--limit-upload "$OFFSITE_LIMIT_UPLOAD_KBPS")
      # BK_NOW (tests): record the injected time so age checks see it
      [ -z "${BK_NOW:-}" ] || args+=(--time "$(date -d "@$BK_NOW" '+%Y-%m-%d %H:%M:%S')")
      log "offsite $set/$app: $src ($name)"
      if (cd "$src" && nicely rs "${args[@]}"); then
        stamp "offsite-$set-$app"
      else
        notify crit "$BRAND_SHORT-offsite-$set-$app" "offsite $set/$app failed on $HOST_LABEL" \
          "restic backup of $src failed; the repository keeps its previous snapshots."
        OFFSITE_FAILURES=$((OFFSITE_FAILURES + 1))
      fi
      src="" name=""
    done
  done
}

cmd_offsite() {
  [ "$OFFSITE_ENABLED" = 1 ] || { log "offsite tier disabled"; return 0; }
  take_lock
  if sets_need_data_snapshot; then run_snapshot_begin; fi
  offsite_sets
  run_snapshot_end
  [ "$OFFSITE_FAILURES" = 0 ]
}

cmd_init_offsite() {
  offsite_env
  if rs cat config >/dev/null 2>&1; then
    log "restic repository already initialised"
  else
    rs init
  fi
}

cmd_snapshots() {
  offsite_env
  rs snapshots --host "$HOST_LABEL" "$@"
}

cmd_prune() {
  [ "$OFFSITE_ENABLED" = 1 ] || { log "offsite tier disabled"; return 0; }
  take_lock
  offsite_env
  local rec app failures=0
  for rec in "${OFFSITE_SETS[@]}"; do
    record "$rec"
    split_commas "${REC_FIELDS[2]:-}"
    for app in "${COMMA_FIELDS[@]}"; do
      # --group-by host: the frozen path differs every night, so the default
      # host,paths grouping would keep every snapshot forever
      rs forget --host "$HOST_LABEL" --tag "set:${REC_FIELDS[0]},app:$app" --group-by host \
        "${OFFSITE_FORGET_ARGS[@]}" || failures=$((failures + 1))
    done
  done
  nicely rs prune || failures=$((failures + 1))
  if [ "$failures" != 0 ]; then
    notify crit "$BRAND_SHORT-prune" "offsite prune failed on $HOST_LABEL" "restic forget/prune reported $failures failure(s)."
    return 1
  fi
  stamp prune
}

# --- tier 5 -----------------------------------------------------------------

cmd_restore_test() {
  [ "$OFFSITE_ENABLED" = 1 ] || { log "offsite tier disabled: nothing to restore-test"; return 0; }
  take_lock
  offsite_env
  local rec set source app latest id name srcdir lines=() result=PASS path remote localsum n compared
  for rec in "${OFFSITE_SETS[@]}"; do
    record "$rec"
    set=${REC_FIELDS[0]} source=${REC_FIELDS[1]}
    split_commas "${REC_FIELDS[2]:-}"
    for app in "${COMMA_FIELDS[@]}"; do
      if ! latest=$(offsite_latest "$set" "$app") || [ -z "$latest" ]; then
        lines+=("FAIL $set/$app: no offsite snapshot"); result=FAIL; continue
      fi
      read -r id _ name <<<"$latest"
      srcdir=""
      case "$source" in
        copy) snap_exists "$COPY_TARGET/$app" "$name" && srcdir=$(snap_path "$COPY_TARGET/$app" "$name") ;;
        data) snap_exists "$DATA_TARGET" "$name" && srcdir="$(snap_path "$DATA_TARGET" "$name")/$app" ;;
      esac
      n=0 compared=0
      while IFS= read -r path; do
        [ -n "$path" ] || continue
        n=$((n + 1))
        if ! remote=$(rs dump "$id" "$path" | sha256sum | cut -c1-64); then
          lines+=("FAIL $set/$app: could not restore $path"); result=FAIL; continue
        fi
        [ -n "$srcdir" ] || continue
        localsum=$(sha256sum <"$srcdir$path" | cut -c1-64)
        compared=$((compared + 1))
        if [ "$remote" != "$localsum" ]; then
          lines+=("FAIL $set/$app: $path differs from the frozen source $name"); result=FAIL
        fi
      done < <(rs ls --json "$id" | jq -r 'select(.type == "file") | .path' | shuf -n "$RESTORE_TEST_SAMPLES")
      if [ "$n" = 0 ]; then
        lines+=("FAIL $set/$app: snapshot $id holds no files"); result=FAIL
      elif [ -n "$srcdir" ]; then
        lines+=("ok   $set/$app: $n file(s) restored, $compared sha256-matched against $name")
      else
        lines+=("note $set/$app: $n file(s) restored; frozen source $name no longer exists, not compared")
      fi
    done
  done
  if rs check --read-data-subset="$RESTORE_TEST_READ_SUBSET" >/dev/null 2>&1; then
    lines+=("ok   repository check, data subset $RESTORE_TEST_READ_SUBSET")
  else
    lines+=("FAIL repository check, data subset $RESTORE_TEST_READ_SUBSET"); result=FAIL
  fi
  local hname htmo hcmd
  for rec in "${RESTORE_TEST_HOOKS[@]}"; do
    IFS='|' read -r hname htmo hcmd <<<"$rec"
    if timeout "$htmo" bash -c "$hcmd"; then lines+=("ok   hook $hname"); else lines+=("FAIL hook $hname"); result=FAIL; fi
  done
  lines+=("" "RESULT: $result")
  local report body
  report=$(printf '%s\n' "${lines[@]}" | report_write "restore-test-$(ts).txt" "restore test")
  body=$(cat "$report")
  printf '%s\n' "$body"
  if [ "$result" = PASS ]; then
    notify info "$BRAND_SHORT-restore-test" "restore test passed on $HOST_LABEL" "$body"
    stamp restore-test
  else
    notify crit "$BRAND_SHORT-restore-test" "restore test FAILED on $HOST_LABEL" "$body"
    return 1
  fi
}

# --- restore ----------------------------------------------------------------

restore_guard() {  # restore_guard DEST -- never into the live data, never over existing files
  local dest data
  dest=$(realpath -m "$1")
  data=$(realpath -m "$(target_path "$DATA_TARGET")")
  case "$dest/" in
    "$data"/*) usage_die "refusing to restore into the live data path $dest: restore beside it, then swap under maintenance" ;;
  esac
  if [ -d "$dest" ] && [ -n "$(ls -A "$dest")" ]; then
    usage_die "restore target $dest is not empty"
  fi
  mkdir -p "$dest"
}

cmd_restore() {
  local source=${1:-}
  case "$source" in
    copy | snapshot)
      [ $# -eq 4 ] || usage_die "restore $source APP SNAPSHOT DIR"
      local app=$2 name=$3 dest=$4 src
      if [ "$source" = copy ]; then
        snap_exists "$COPY_TARGET/$app" "$name" || usage_die "no snapshot $name of $COPY_TARGET/$app"
        src=$(snap_path "$COPY_TARGET/$app" "$name")
      else
        snap_exists "$DATA_TARGET" "$name" || usage_die "no snapshot $name of $DATA_TARGET"
        src="$(snap_path "$DATA_TARGET" "$name")/$app"
      fi
      [ -d "$src" ] || usage_die "$src does not exist"
      restore_guard "$dest"
      nicely rsync -a "$src/" "$dest/"
      log "restored $src -> $dest"
      ;;
    offsite)
      [ $# -eq 4 ] || [ $# -eq 5 ] || usage_die "restore offsite SET APP DIR [SNAPSHOT-ID]"
      local set=$2 app=$3 dest=$4 id=${5:-} latest
      offsite_env
      if [ -z "$id" ]; then
        latest=$(offsite_latest "$set" "$app") || die "cannot list offsite snapshots"
        [ -n "$latest" ] || die "no offsite snapshot of $set/$app"
        id=${latest%% *}
      fi
      restore_guard "$dest"
      rs restore "$id" --target "$dest"
      log "restored offsite $set/$app snapshot $id -> $dest"
      ;;
    *) usage_die "restore copy|snapshot|offsite ..." ;;
  esac
}
