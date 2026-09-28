# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Snapshot backends. A TARGET is a ZFS dataset (SNAP_BACKEND=zfs) or a
# directory (SNAP_BACKEND=dir). Every tier goes through these functions, so
# the same engine runs on a ZFS storage host and on a test VM without ZFS.
#
#   zfs  snapshots are ZFS snapshots (atomic, no I/O); a snapshot is read
#        through <mountpoint>/.zfs/snapshot/<name>.
#   dir  snapshots are rsync --link-dest trees under SNAP_DIR_ROOT: files
#        unchanged since the previous snapshot are hard links to IT (never
#        to the live tree), so a snapshot is frozen against later writes.
#        The first snapshot of a target is a full copy. Meant for hosts and
#        test clusters without ZFS, not for terabytes.

_dir_root() {  # _dir_root TARGET -> where the dir backend keeps its snapshots
  local t=${1#/}
  printf '%s/%s\n' "$SNAP_DIR_ROOT" "${t//\//_}"
}

target_exists() {
  if [ "$SNAP_BACKEND" = zfs ]; then zfs list -H -o name "$1" >/dev/null 2>&1; else [ -d "$1" ]; fi
}

target_ensure() {  # create a copy-tier target (a child dataset or a directory)
  if [ "$SNAP_BACKEND" = zfs ]; then
    target_exists "$1" || zfs create -p "$1"
  else
    mkdir -p "$1"
  fi
}

target_path() {  # the live, mounted path of a target
  if [ "$SNAP_BACKEND" = zfs ]; then zfs get -H -o value mountpoint "$1"; else printf '%s\n' "$1"; fi
}

snap_list() {  # snap_list TARGET -> snapshot names, oldest first ("" if the target has none)
  if [ "$SNAP_BACKEND" = zfs ]; then
    target_exists "$1" || return 0
    zfs list -H -t snapshot -o name -s creation -d 1 "$1" | sed -n "s|^$1@||p"
  else
    local root meta
    root=$(_dir_root "$1")
    [ -d "$root/.meta" ] || return 0
    for meta in "$root"/.meta/*; do
      [ -f "$meta" ] || continue
      printf '%s %s\n' "$(cat "$meta")" "${meta##*/}"
    done | sort -n -k1,1 -k2,2 | cut -d' ' -f2
  fi
}

snap_created() {  # snap_created TARGET NAME -> epoch
  if [ "$SNAP_BACKEND" = zfs ]; then
    zfs get -Hp -o value creation "$1@$2"
  else
    cat "$(_dir_root "$1")/.meta/$2"
  fi
}

snap_path() {  # snap_path TARGET NAME -> a readable path of the snapshot's tree
  if [ "$SNAP_BACKEND" = zfs ]; then
    printf '%s/.zfs/snapshot/%s\n' "$(target_path "$1")" "$2"
  else
    printf '%s/%s\n' "$(_dir_root "$1")" "$2"
  fi
}

snap_exists() {
  if [ "$SNAP_BACKEND" = zfs ]; then
    zfs list -H -t snapshot -o name "$1@$2" >/dev/null 2>&1
  else
    [ -f "$(_dir_root "$1")/.meta/$2" ]
  fi
}

snap_create() {  # snap_create TARGET NAME
  local target=$1 name=$2
  if snap_exists "$target" "$name"; then
    warn "snapshot $target@$name already exists"
    return 1
  fi
  if [ "$SNAP_BACKEND" = zfs ]; then
    zfs snapshot "$target@$name"
    return
  fi
  local root prev args=(-a --delete)
  root=$(_dir_root "$target")
  mkdir -p "$root/.meta"
  prev=$(snap_list "$target" | tail -n 1)
  [ -z "$prev" ] || args+=("--link-dest=$root/$prev")
  rm -rf "${root:?}/$name.partial"
  nicely rsync "${args[@]}" "$target/" "$root/$name.partial/"
  mv "$root/$name.partial" "$root/$name"
  now >"$root/.meta/$name"
}

snap_destroy() {  # snap_destroy TARGET NAME
  if [ "$SNAP_BACKEND" = zfs ]; then
    zfs destroy "$1@$2"
  else
    local root
    root=$(_dir_root "$1")
    rm -f "$root/.meta/$2"
    rm -rf "${root:?}/${2:?}"
  fi
}

prune_keep() {  # prune_keep TARGET PREFIX KEEP -> destroy all but the newest KEEP snapshots named PREFIX*
  local target=$1 prefix=$2 keep=$3 names=() n i
  while IFS= read -r n; do
    [[ $n == "$prefix"* ]] && names+=("$n")
  done < <(snap_list "$target")
  for (( i = 0; i < ${#names[@]} - keep; i++ )); do
    snap_destroy "$target" "${names[$i]}"
    log "pruned $target@${names[$i]}"
  done
}

capacity_pct() {  # capacity_pct TARGET -> used % of the pool or filesystem holding it
  if [ "$SNAP_BACKEND" = zfs ]; then
    zpool list -H -o cap "${1%%/*}" | tr -dc '0-9'
  else
    df --output=pcent "$1" | tail -n 1 | tr -dc '0-9'
  fi
  printf '\n'
}
