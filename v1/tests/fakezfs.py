#!/usr/bin/env python3
"""Test double for the few zfs/zpool/mount commands the skbackup engine runs,
so its real scripts can be exercised on a plain directory tree without root
or ZFS. Invoked through shims named zfs, zpool, mount, umount and
mountpoint (see skbackup_support.FakeHost); state lives in $FAKEZFS_STATE.

Datasets are directories. `zfs snapshot DS@N` copies the dataset's tree
(minus .zfs) to <mountpoint>/.zfs/snapshot/N, so a snapshot is frozen like a
real one. Snapshot creation time is $FAKEZFS_NOW when set (to age a snapshot
on purpose), else now. `mount -t zfs -o ro DS@N DIR` makes DIR a symlink to
that snapshot directory. Only the flag shapes the engine uses are
supported; anything else exits 2 so a new call shape fails loudly."""
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

STATE = Path(os.environ["FAKEZFS_STATE"])


def load():
    return json.loads(STATE.read_text())


def save(st):
    STATE.write_text(json.dumps(st, indent=1))


def die(msg, rc=1):
    print(msg, file=sys.stderr)
    sys.exit(rc)


def snapdir(st, snap):
    ds, name = snap.split("@", 1)
    return Path(st["datasets"][ds]) / ".zfs" / "snapshot" / name


def parse(args):
    flags, opts, pos = set(), {}, []
    it = iter(args)
    for a in it:
        if a in ("-o", "-t", "-s", "-d"):
            opts[a] = next(it)
        elif a.startswith("-") and len(a) > 1:
            flags.update(a[1:])
        else:
            pos.append(a)
    return flags, opts, pos


def zfs(args):
    st = load()
    cmd, rest = args[0], args[1:]
    flags, opts, pos = parse(rest)
    if cmd == "snapshot":
        snaps = []
        for snap in pos:
            ds, name = snap.split('@', 1)
            if ds not in st['datasets']:
                die(f"cannot open '{ds}': dataset does not exist")
            datasets = sorted(d for d in st['datasets'] if d == ds or ('r' in flags and d.startswith(ds+'/')))
            snaps.extend(d+'@'+name for d in datasets)
        # ZFS rejects the entire atomic operation if any requested name exists.
        for snap in snaps:
            if snap in st['snaps']:
                die(f"cannot create snapshot '{snap}': dataset already exists")
        created = int(os.environ.get('FAKEZFS_NOW') or time.time())
        for snap in snaps:
            ds = snap.split('@')[0]
            src = Path(st['datasets'][ds])
            dst = snapdir(st, snap)
            dst.parent.mkdir(parents=True, exist_ok=True)
            children = [Path(m) for d,m in st['datasets'].items() if d.startswith(ds+'/') and Path(m).is_relative_to(src)]
            shutil.copytree(src, dst, symlinks=True, ignore=lambda d,n: [x for x in n if x == '.zfs' or Path(d)/x in children])
            for child in children:
                (dst/child.relative_to(src)).mkdir(parents=True, exist_ok=True)
            st.setdefault('events', []).append(['snapshot', snap])
            st['snaps'][snap] = created
        st.setdefault('snapshot_calls', []).append(rest)
        save(st)
    elif cmd == "destroy":
        snap = pos[0]
        if snap not in st["snaps"]:
            die(f"could not find any snapshots to destroy; check snapshot names.")
        shutil.rmtree(snapdir(st, snap), ignore_errors=True)
        del st["snaps"][snap]
        save(st)
    elif cmd == "create":
        ds = pos[0]
        parts = ds.split("/")
        for i in range(2, len(parts) + 1):
            name = "/".join(parts[:i])
            if name in st["datasets"]:
                continue
            parent = "/".join(parts[:i - 1])
            if parent not in st["datasets"]:
                die(f"cannot create '{ds}': parent does not exist")
            mnt = Path(st["datasets"][parent]) / parts[i - 1]
            mnt.mkdir(parents=True, exist_ok=True)
            st["datasets"][name] = str(mnt)
        save(st)
    elif cmd == "get":
        prop, target = pos
        if prop == "mountpoint":
            if target not in st["datasets"]:
                die(f"cannot open '{target}': dataset does not exist")
            print(st["datasets"][target])
        elif prop == "creation":
            if target not in st["snaps"]:
                die(f"cannot open '{target}': dataset does not exist")
            print(st["snaps"][target])
        else:
            die(f"fakezfs: unsupported property {prop}", 2)
    elif cmd == "list":
        cols = opts.get("-o", "name").split(",")
        target = pos[0]
        if opts.get("-t") == "snapshot":
            if "@" in target:
                rows = [target] if target in st["snaps"] else die(f"cannot open '{target}': dataset does not exist")
            else:
                if target not in st["datasets"]:
                    die(f"cannot open '{target}': dataset does not exist")
                rows = sorted((s for s in st["snaps"] if s.split("@")[0] == target), key=lambda s: (st["snaps"][s], s))
            for s in rows:
                vals = [s if c == "name" else str(st["snaps"][s]) for c in cols]
                print("\t".join(vals))
        else:
            if target not in st["datasets"] and target not in st["snaps"]:
                die(f"cannot open '{target}': dataset does not exist")
            if 'r' in flags:
                for ds in sorted(d for d in st['datasets'] if (d==target or d.startswith(target+'/')) and
                                 (opts.get('-t') != 'filesystem' or d not in st.get('volumes', []))):
                    print(ds)
            else:
                print(target)
    else:
        die(f"fakezfs: unsupported zfs {cmd}", 2)


def zpool(args):
    st = load()
    flags, opts, pos = parse(args[1:])
    pool = st["pools"].get(pos[0])
    if pool is None:
        die(f"cannot open '{pos[0]}': no such pool")
    col = opts["-o"]
    print({"health": pool["health"], "capacity": pool["cap"] + "%", "cap": pool["cap"] + "%"}[col])


def mount(args):
    flags, opts, pos = parse(args)
    snap, mnt = pos
    st = load()
    if opts.get('-o') != 'ro' or snap not in st['snaps']:
        die('fakezfs: only existing read-only snapshots can be mounted', 2)
    target = snapdir(st, snap)
    m = Path(mnt)
    ds = snap.split('@')[0]
    use_overlay = bool(st.get('mounts')) or any(d.startswith(ds+'/') for d in st['datasets'])
    if use_overlay:
        stash = STATE.parent/'mount-stash'/uuid.uuid4().hex
        stash.parent.mkdir(exist_ok=True)
        m.rename(stash)
        shutil.copytree(target,m,symlinks=True)
        st.setdefault('mounts', {})[str(m)] = str(stash)
    else:
        m.rmdir()
        m.symlink_to(target)
        st.setdefault('mounts', {})[str(m)] = ''
    st.setdefault('events', []).append(['mount',str(m)])
    save(st)


def umount(args):
    m = Path(args[-1]);st=load()
    if str(m) not in st.get('mounts', {}):
        die('fakezfs: not mounted',32)
    stash=st['mounts'].pop(str(m))
    if stash:
        shutil.rmtree(m)
        Path(stash).rename(m)
    else:
        m.unlink();m.mkdir()
    st.setdefault('events', []).append(['umount',str(m)])
    save(st)


def mountpoint(args):
    sys.exit(0 if str(Path(args[-1])) in load().get('mounts',{}) else 1)


if __name__ == "__main__":
    prog = Path(sys.argv[1]).name
    {"zfs": zfs, "zpool": zpool, "mount": mount, "umount": umount,
     "mountpoint": mountpoint}[prog](sys.argv[2:])
