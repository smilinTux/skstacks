"""Shared helpers for the skbackup tests (not a test module itself).

Two ways to get skbackup artifacts:

* ``ansible_render(tmp, vault)`` runs the REAL deploy playbook through the
  render gate (render/render_playbooks.py) with ``vault`` as the instance
  vault and returns the output dir: every template the deploy would write,
  keyed by its destination path, plus the engine tree the deploy copies
  (under ``_copy/``) and every shell task body (under ``_shell/``). The
  playbook, not a re-implementation of it, decides names, paths and units.
* ``Engine`` drives the static engine (src/skbackup/backup) against a
  scratch tree with the ``dir`` snapshot backend: no ZFS, no root.
"""
from __future__ import annotations

import copy
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

TESTS = Path(__file__).resolve().parent
V1 = TESTS.parent
APP = V1 / "ansible" / "optional" / "skbackup"
ENGINE = APP / "src" / "skbackup"
BACKUP = ENGINE / "backup"
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
    """Every tier on, zfs backend, placeholder secrets."""
    vault = {
        "data_target": "tank/data",
        "copy": {"enabled": True, "target": "backup/copies",
                 "apps": [{"name": "app-one", "excludes": ["/cache"], "retention": {"daily": 14}},
                          {"name": "app-two"}]},
        "dumps": {"enabled": True,
                  "hooks": [{"name": "app-one-db", "command": "/usr/local/bin/dump-app-one --out /data/app-one/dump"}],
                  "checks": [{"app": "app-one", "glob": "dump/*.sql.gz", "max_age_hours": 26}]},
        "offsite": {"enabled": True, "repository": "s3:https://s3.example.test/bucket/host",
                    "password": "example-restic-password-not-a-secret",
                    "env": {"AWS_ACCESS_KEY_ID": "example-key-id", "AWS_SECRET_ACCESS_KEY": "example-key-not-a-secret"},
                    "sets": [{"name": "keep", "source": "copy", "apps": ["app-one", "app-two"], "tags": ["nightly"]},
                             {"name": "photos", "source": "data", "apps": ["app-three"], "excludes": ["thumbs"]}]},
        "restore_test": {"enabled": True},
        "alerts": {"enabled": True},
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
        elif isinstance(v, (list, tuple)):
            lines.append(f"{k}=(" + " ".join(shlex.quote(str(x)) for x in v) + ")")
        else:
            lines.append(f"{k}={shlex.quote(str(v))}")
    return "\n".join(lines) + "\n"


class Engine:
    """The static engine against a scratch tree (dir backend)."""

    def __init__(self, root: Path, brand: dict | None = None, **conf):
        self.root = root
        self.data = root / "data"
        self.copies = root / "copies"
        self.state = root / "state"
        self.repo = root / "repo"
        for d in (self.data, self.copies, self.state):
            d.mkdir(parents=True, exist_ok=True)
        (root / "etc").mkdir(exist_ok=True)
        self.pass_file = root / "etc" / "offsite.pass"
        self.pass_file.write_text("test-restic-password\n")
        self.env_file = root / "etc" / "offsite.env"
        self.env_file.write_text(conf_text(RESTIC_REPOSITORY=str(self.repo), RESTIC_PASSWORD_FILE=str(self.pass_file)))
        b = deep_merge(DEFAULTS["branding"], brand or {})
        base = dict(
            BRAND_PRODUCT=b["product_name"], BRAND_SHORT=b["short_name"], BRAND_TAGLINE=b["tagline"],
            BRAND_ALERT_PREFIX=b["alert_prefix"], BRAND_FOOTER=b["report_footer"], BRAND_CONTACT=b["contact"],
            BRAND_LOGO_URL=b["logo_url"], BRAND_CREDITS_TEXT=b["credits_text"] if b["credits"] else "",
            UNIT_BASE=b["short_name"], HOST_LABEL="test-host",
            STATE_DIR=str(self.state), LOCK_FILE=str(root / "lock"), REPORT_DIR=str(self.state / "reports"),
            SNAP_BACKEND="dir", SNAP_DIR_ROOT=str(root / "snaps"), SNAP_PREFIX=b["short_name"],
            DATA_TARGET=str(self.data), IONICE=False,
            SNAPSHOTS_ENABLED=True, SNAPSHOTS_ENGINE="builtin", SNAPSHOTS_KEEP=3,
            PREDEPLOY_KEEP_DAYS=30, PREDEPLOY_KEEP_MIN=2,
            COPY_ENABLED=True, COPY_TARGET=str(self.copies), COPY_BWLIMIT_KBPS=0,
            COPY_APPS=["app1|7|4|3|/cache", "app2|7|4|3"],
            DUMPS_ENABLED=False, DUMPS_REQUIRED=True, DUMP_HOOKS=[], DUMP_CHECKS=[],
            OFFSITE_ENABLED=False, OFFSITE_ENV_FILE=str(self.env_file), OFFSITE_LIMIT_UPLOAD_KBPS=0,
            OFFSITE_TAGS=[b["short_name"]], OFFSITE_SETS=["keep|copy|app1,app2||"], OFFSITE_IGNORE_INODE=True,
            OFFSITE_FORGET_ARGS=["--keep-daily", "14"],
            RESTORE_TEST_ENABLED=False, RESTORE_TEST_SAMPLES=20, RESTORE_TEST_READ_SUBSET="100%", RESTORE_TEST_HOOKS=[],
            ALERTS_ENABLED=True, NOTIFY_MODE="log", NOTIFY_LOG=str(self.state / "alerts.log"), NOTIFY_COMMAND="",
            MAX_AGE_SNAPSHOT_H=2, MAX_AGE_COPY_H=26, MAX_AGE_DUMP_H=26, MAX_AGE_OFFSITE_H=26,
            MAX_AGE_RESTORE_TEST_H=840, POOL_CAP_WARN_PCT=101,
        )
        base.update(conf)
        self.conf = root / "etc" / "backup.conf"
        self.conf.write_text(conf_text(**base))
        self.values = base

    def seed(self):
        """Two apps with a few files, one excluded dir, one binary blob."""
        (self.data / "app1" / "sub").mkdir(parents=True, exist_ok=True)
        (self.data / "app1" / "cache").mkdir(exist_ok=True)
        (self.data / "app1" / "a.txt").write_text("alpha\n")
        (self.data / "app1" / "sub" / "b.bin").write_bytes(os.urandom(200_000))
        (self.data / "app1" / "cache" / "junk").write_text("regenerable\n")
        (self.data / "app2").mkdir(exist_ok=True)
        (self.data / "app2" / "c.txt").write_text("gamma\n")
        (self.data / "app3").mkdir(exist_ok=True)
        (self.data / "app3" / "photo.jpg").write_bytes(os.urandom(50_000))
        return self

    def run(self, *args, now: int | None = None, check: bool = False, env: dict | None = None):
        e = dict(os.environ)
        e.pop("BK_NOW", None)
        if now is not None:
            e["BK_NOW"] = str(now)
        e.update(env or {})
        proc = subprocess.run(["bash", str(BACKUP), "--conf", str(self.conf), *args],
                              capture_output=True, text=True, env=e, timeout=300)
        if check and proc.returncode != 0:
            raise AssertionError(f"{args} rc={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        return proc

    def alerts(self) -> str:
        p = self.state / "alerts.log"
        return p.read_text() if p.exists() else ""

    def snaps(self, target: Path) -> list[str]:
        d = self.root / "snaps" / str(target).strip("/").replace("/", "_")
        return sorted(p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")) if d.exists() else []
