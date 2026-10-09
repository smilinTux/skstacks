"""Shared helpers for the skbackup tests (not a test module itself).

Two ways to get skbackup artifacts:

* ``ansible_render(tmp, vault)`` runs the REAL deploy playbook through the
  render gate (render/render_playbooks.py) with ``vault`` as the instance
  vault and returns the output dir: every template the deploy would write,
  keyed by its destination path, plus the engine tree the deploy copies
  (under ``_copy/``) and every shell task body (under ``_shell/``). The
  playbook, not a re-implementation of it, decides names, paths and units.
* ``FakeHost`` drives the engine (src/skbackup) against a scratch tree:
  zfs, zpool, mount, umount and mountpoint are test doubles (fakezfs.py)
  put first on the engine's PATH through BACKUP_TEST_PATH; everything else
  (rsync, restic, flock, find, sha256sum) is real.
"""
from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

TESTS = Path(__file__).resolve().parent
V1 = TESTS.parent
APP = V1 / "ansible" / "optional" / "skbackup"
ENGINE = APP / "src" / "skbackup"
BACKUP = ENGINE / "backup"
LIB = ENGINE / "lib.sh"
DEFAULTS = yaml.safe_load((APP / "vars" / "defaults.yml").read_text())["skbackup_defaults"]

sys.path.insert(0, str(TESTS / "render"))
import render_playbooks as rp  # noqa: E402

# The default brand strings. With a custom brand set, none of these may
# appear in anything the deploy puts on the host or the engine prints.
DEFAULT_BRAND_PATTERNS = [
    "skbackup", "skstacks", "smilintux", "sk-alert",
    DEFAULTS["branding"]["product_name"].lower(),
    DEFAULTS["branding"]["tagline"].lower(),
    DEFAULTS["branding"]["report_footer"].lower(),
    DEFAULTS["branding"]["alert_prefix"].lower(),
]

WHITE_LABEL = {
    "product_name": "Acme Vault",
    "short_name": "acmevault",
    "tagline": "Your data, kept safe by Acme IT",
    "alert_prefix": "ACME-VAULT",
    "report_footer": "Acme IT managed backup service",
    "contact": "support@acme.example",
    "logo_url": "https://acme.example/logo.png",
    "credits": False,
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def full_vault(**over) -> dict:
    """Every tier on, placeholder secrets."""
    vault = {
        "data_dataset": "tank/data",
        "copy": {"enabled": True, "target": "backup/copies",
                 "apps": [{"name": "app-one", "excludes": ["/cache/"], "retention": {"daily": 14},
                           "newest": [{"dir": "database-dump", "keep": 2}], "dump": "database-dump"},
                          {"name": "app-two", "src": "runtime/app-two"}]},
        "dumps": {"hooks": [{"name": "app-one-db", "command": "/usr/local/bin/dump-app-one --out /tank/data/app-one/database-dump"}]},
        "offsite": {"enabled": True, "repository": "s3:https://s3.example.test/bucket",
                    "password": "example-restic-password-not-a-secret",
                    "env": {"B2_ACCOUNT_ID": "example-key-id", "B2_ACCOUNT_KEY": "example-key-not-a-secret"},
                    "sets": [{"name": "kept-apps", "kind": "apps"},
                             {"name": "photos", "kind": "paths", "paths": ["app-three/originals", "app-three/raw"],
                              "excludes": ["/app-three/originals/tmp"]}]},
        "restore_test": {"enabled": True, "app": "app-one", "sample_set": "photos"},
        "alerts": {"notifier": "log"},
    }
    return deep_merge(vault, over)


def have_ansible() -> bool:
    return shutil.which("ansible-playbook") is not None


needs_ansible = pytest.mark.skipif(not have_ansible(), reason="ansible-playbook not installed")
needs_restic = pytest.mark.skipif(shutil.which("restic") is None, reason="restic not installed")


def ansible_render(tmp: Path, vault: dict, env: str = "prod") -> tuple[dict, Path]:
    """Render deploy_skbackup-<env>.yml through real Ansible with `vault` as
    the skbackup vault. Returns (render result, output dir)."""
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    (vars_dir / "skbackup.example.yml").write_text(yaml.safe_dump({"skbackup": vault}))
    run_dir = work / f"skbackup-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "skbackup" / f"deploy_skbackup-{env}.yml"
        res = rp.run_one(work, "optional", "skbackup", env, pb, "base")
    finally:
        rp.VARS = saved
    res["log"] = (run_dir / "ansible.log").read_text() if (run_dir / "ansible.log").exists() else ""
    return res, run_dir / "out"


def rendered(out: Path) -> dict[str, str]:
    """{relative path: text} for every rendered file."""
    files = {}
    for p in sorted(out.rglob("*")):
        if p.is_file():
            try:
                files[str(p.relative_to(out))] = p.read_text()
            except UnicodeDecodeError:
                files[str(p.relative_to(out))] = ""
    return files


def source_conf(path: Path, *names: str) -> dict[str, str]:
    """Source a rendered config in bash and read back variables (arrays
    joined with newlines)."""
    script = f"set -eu; . {shlex.quote(str(path))}\n"
    for n in names:
        script += f'if declare -p {n} 2>/dev/null | grep -q "^declare -a"; then printf "%s\\n" "${{{n}[@]}}"; else printf "%s\\n" "${{{n}}}"; fi; printf "\\0"\n'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    parts = out.split("\0")
    return {n: parts[i].rstrip("\n") for i, n in enumerate(names)}


def conf_text(**values) -> str:
    """A bash config file. Lists become arrays, bools 1/0."""
    lines = []
    for k, v in values.items():
        if isinstance(v, bool):
            lines.append(f"{k}={1 if v else 0}")
        else:
            lines.append(f"{k}={shlex.quote(str(v))}")
    return "\n".join(lines) + "\n"


FAKEZFS = TESTS / "fakezfs.py"


class FakeHost:
    """A storage host on a scratch tree: pool `tank` with dataset tank/data
    (the swarm's data) and pool `backup` with backup/copies (the copy
    pool). zfs/zpool/mount are fakezfs.py; the engine is the real one."""

    def __init__(self, root: Path, brand: dict | None = None, apps: list[str] | None = None,
                 sets: list[str] | None = None, hooks: list[str] | None = None, **conf):
        self.root = root
        self.data = root / "tank" / "data"
        self.copies = root / "backup" / "copies"
        self.state = root / "state"
        self.repo = root / "repo"
        self.mnt = root / "run" / "src"
        self.bin = root / "fakebin"
        for d in (self.data, self.copies, self.state, self.bin, root / "etc", root / "lock", root / "cache"):
            d.mkdir(parents=True, exist_ok=True)
        for name in ("zfs", "zpool", "mount", "umount", "mountpoint", "mkdir"):
            shim = self.bin / name
            shim.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKEZFS} "$0" "$@"\n')
            shim.chmod(0o755)
        self.zstate = root / "fakezfs.json"
        self.zstate.write_text(json.dumps({
            "datasets": {"tank": str(root / "tank"), "tank/data": str(self.data),
                         "backup": str(root / "backup"), "backup/copies": str(self.copies)},
            "snaps": {},
            "pools": {"tank": {"health": "ONLINE", "cap": "12"}, "backup": {"health": "ONLINE", "cap": "9"}},
        }))
        etc = root / "etc"
        (etc / "apps.conf").write_text("\n".join(apps if apps is not None else [
            "app1 app1 7 4 3 --exclude=/cache/ newest=dump:2",
            "app2 app2 7 4 3",
        ]) + "\n")
        (etc / "restic-sets.conf").write_text("\n".join(sets if sets is not None else [
            "kept apps", "photos paths app3",
        ]) + "\n")
        (etc / "hooks.conf").write_text("\n".join(hooks or []) + "\n")
        self.env_file = etc / "offsite.env"
        self.env_file.write_text(conf_text(RESTIC_REPOSITORY=str(self.repo), RESTIC_PASSWORD="test-restic-password"))
        b = deep_merge(DEFAULTS["branding"], brand or {})
        base = dict(
            BRAND_PRODUCT=b["product_name"], BRAND_SHORT=b["short_name"], BRAND_TAGLINE=b["tagline"],
            BRAND_ALERT_PREFIX=b["alert_prefix"], BRAND_FOOTER=b["report_footer"], BRAND_CONTACT=b["contact"],
            BRAND_LOGO_URL=b["logo_url"], BRAND_CREDITS_TEXT=b["credits_text"] if b["credits"] else "",
            UNIT_BASE=b["short_name"], RESTIC_HOST="test-host",
            SRC_DATASET="tank/data", BAK_ROOT="backup/copies", APPS_FILE=str(etc / "apps.conf"),
            HOOKS_FILE=str(etc / "hooks.conf"), SNAP_PREFIX_SYNC=b["short_name"], RSYNC_BWLIMIT=0,
            DUMP_MAX_AGE_H=26, RESTIC_ENV_FILE=str(self.env_file), RESTIC_SETS_FILE=str(etc / "restic-sets.conf"),
            RESTIC_LIMIT_UPLOAD=0, RESTIC_CACHE_DIR=str(root / "cache"), RESTIC_SRC_MNT=str(self.mnt),
            RESTORE_TEST_APP="app1", RESTORE_TEST_SAMPLE_TAG="photos", RESTORE_TEST_SAMPLES=5,
            STATE_DIR=str(self.state), REPORT_DIR=str(self.state / "reports"), LOCK_DIR=str(root / "lock"),
            MAX_AGE_SANOID_MIN=120, MAX_AGE_SKBAK_H=26, MAX_AGE_RESTIC_H=36, CHECK_RESTIC_REPO=0,
            POOL_CAP_WARN=80, THINPOOL="", NOTIFY_MODE="log", NOTIFY_LOG=str(self.state / "alerts.log"),
            NOTIFY_COMMAND="",
        )
        base.update(conf)
        self.conf = etc / "backup.conf"
        self.conf.write_text(conf_text(**base))

    def env(self, now: int | None = None, extra: dict | None = None) -> dict:
        e = dict(os.environ, BACKUP_TEST_PATH=f"{self.bin}:{os.environ['PATH']}", FAKEZFS_STATE=str(self.zstate))
        e.pop("FAKEZFS_NOW", None)
        if now is not None:
            e["FAKEZFS_NOW"] = str(now)
        e.update(extra or {})
        return e

    def run(self, *args, now: int | None = None, check: bool = False, env: dict | None = None):
        proc = subprocess.run(["bash", str(BACKUP), "--conf", str(self.conf), *args],
                              capture_output=True, text=True, env=self.env(now, env), timeout=300)
        if check and proc.returncode != 0:
            raise AssertionError(f"{args} rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        return proc

    def zfs(self, *args, now: int | None = None):
        return subprocess.run([str(self.bin / "zfs"), *args], capture_output=True, text=True,
                              env=self.env(now), check=True).stdout

    def sanoid(self, now: int | None = None, name: str = "autosnap_2027-01-15_08:00:00_hourly"):
        self.zfs("snapshot", f"tank/data@{name}", now=now)

    def snaps(self, dataset: str) -> list[str]:
        st = json.loads(self.zstate.read_text())
        return sorted((s.split("@", 1)[1] for s in st["snaps"] if s.split("@")[0] == dataset),
                      key=lambda n: st["snaps"][f"{dataset}@{n}"])

    def set_creation(self, snap: str, epoch: int):
        st = json.loads(self.zstate.read_text())
        st["snaps"][snap] = epoch
        self.zstate.write_text(json.dumps(st))

    def set_pool(self, pool: str, **kv):
        st = json.loads(self.zstate.read_text())
        st["pools"][pool].update(kv)
        self.zstate.write_text(json.dumps(st))

    def alerts(self) -> str:
        p = self.state / "alerts.log"
        return p.read_text() if p.exists() else ""

    def seed(self):
        """Three apps: files, a cache dir app1 excludes, three dumps (newest 2 kept), photos."""
        (self.data / "app1" / "sub").mkdir(parents=True, exist_ok=True)
        (self.data / "app1" / "cache").mkdir(exist_ok=True)
        (self.data / "app1" / "dump").mkdir(exist_ok=True)
        (self.data / "app1" / "a.txt").write_text("alpha\n")
        (self.data / "app1" / "sub" / "b.bin").write_bytes(os.urandom(200_000))
        (self.data / "app1" / "cache" / "junk").write_text("regenerable\n")
        for i, age in enumerate((3, 2, 1)):
            f = self.data / "app1" / "dump" / f"db-{i}.sql"
            f.write_text(f"dump {i}\n")
            t = time.time() - age * 3600
            os.utime(f, (t, t))
        (self.data / "app2").mkdir(exist_ok=True)
        (self.data / "app2" / "c.txt").write_text("gamma\n")
        (self.data / "app3").mkdir(exist_ok=True)
        for i in range(8):
            (self.data / "app3" / f"p{i}.jpg").write_bytes(os.urandom(20_000))
        return self
