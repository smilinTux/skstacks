"""A deploy.j2 must not force-restart a service on its FIRST install.

Most v1 deploy scripts run `docker stack deploy` and then
`docker service update --force` on the stack's services. On a redeploy that
restart is useful (it picks up changed bind-mounted config files, which do
not change the service spec). On a fresh install it kills the first
containers in the middle of their first-boot init: schema migrations,
initial admin/setup, database initdb. Proven for Vikunja (skboard): an
interrupted SQLite migration left a corrupt db and every later start failed.
Forgejo, Authentik, n8n, BookStack, Nextcloud, Immich and the bundled
databases all run non-idempotent first-boot init and are exposed the same way.

Fix: before `docker stack deploy`, each script records which of the services
it later force-updates already exist (`record_preexisting`, which runs
`docker service inspect`). After the deploy it force-updates only those
(`preexisted <svc> && timeout ... docker service update --force ... || true`)
and prints that the first install skips the restart for the new ones.

This file checks that statically for every deploy.j2 that force-updates, and
behaviourally for three representative scripts against a fake `docker`
(absent before deploy: no forced update; present: forced update).
"""
import os
import pathlib
import re
import stat
import subprocess

import jinja2
import pytest

V1_DIR = pathlib.Path(__file__).resolve().parents[1]
DEPLOY_FILES = sorted((V1_DIR / "ansible").glob("*/*/src/*/deploy.j2"))

# skboard is being fixed on its own as the v2.19.1 hotfix (release/v2.19.1)
# and reaches this branch via a merge. Drop it from this set once that merge
# lands so the static check covers it too.
PENDING_MERGE = {"skboard"}

FORCE_RE = re.compile(r"\bdocker service update\b[^\n]*--force\b")
STACK_DEPLOY_RE = re.compile(r"\bdocker stack deploy\b")
GUARD_RE = re.compile(r"\bpreexisted\s+(\S+)\s*&&[^\n]*\bdocker service update\b[^\n]*--force\b[^\n]*\s([^\s>]+)\s*(?:2>/dev/null\s*)?\|\|\s*true\s*$")


def _code_lines(path):
    return [
        (n, line)
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _force_update_files():
    out = []
    for path in DEPLOY_FILES:
        if any(FORCE_RE.search(line) for _, line in _code_lines(path)):
            app = path.parent.name
            marks = ()
            if app in PENDING_MERGE:
                marks = pytest.mark.skip(reason=f"{app} is fixed on release/v2.19.1; covered after merge")
            out.append(pytest.param(path, id=app, marks=marks))
    return out


FILES = _force_update_files()


def test_discovered_force_update_scripts():
    # Sanity floor so a future scan change cannot silently match nothing.
    assert len(FILES) >= 24


def _strip_quotes(s):
    return s.strip('"')


@pytest.mark.parametrize("path", FILES)
def test_force_update_only_for_services_that_preexisted(path):
    text = path.read_text()
    lines = _code_lines(path)
    assert re.search(r"^record_preexisting\(\)", text, re.M), "missing record_preexisting helper"
    assert re.search(r"^preexisted\(\)", text, re.M), "missing preexisted helper"

    deploy_line = next(n for n, line in lines if STACK_DEPLOY_RE.search(line))
    record_lines = [
        n for n, line in lines
        if re.search(r"\brecord_preexisting\b", line) and not line.startswith("record_preexisting()")
    ]
    assert record_lines and min(record_lines) < deploy_line, (
        f"record_preexisting must run before `docker stack deploy` (line {deploy_line})"
    )

    for n, line in lines:
        if not FORCE_RE.search(line):
            continue
        m = GUARD_RE.search(line)
        assert m, f"line {n}: forced update not guarded by `preexisted <svc> &&`: {line.strip()}"
        assert _strip_quotes(m.group(1)) == _strip_quotes(m.group(2)), (
            f"line {n}: preexisted checks {m.group(1)} but updates {m.group(2)}"
        )


FAKE_DOCKER = r"""#!/bin/bash
case "$*" in
  "stack deploy"*)
    touch "$DEPLOYED" ;;
  "service inspect"*)
    if [ "$PRESENT" = 1 ] || [ -e "$DEPLOYED" ]; then echo '[{}]'; exit 0; fi
    echo "Error: no such service" >&2; exit 1 ;;
  "service update"*)
    echo "$*" >> "$UPDATELOG" ;;
  "service ls"*".Name"*)
    if [ "$PRESENT" = 1 ] || [ -e "$DEPLOYED" ]; then printf '%s\n' $SERVICES; fi ;;
  "service ls"*"--format"*)
    echo "1/1" ;;
  *) : ;;
esac
"""

# (app, expected force-updated services for env=dev, compose file name)
CASES = [
    ("skbook", ["skbook-dev_bookstack", "skbook-dev_db", "skbook-dev_db-backup"], "docker-compose.yml"),
    ("skgit", ["skgit-dev_forgejo", "skgit-dev_postgres", "skgit-dev_db-backup"], "docker-compose.yml"),
    ("skgallery", [
        "skgallery-dev_immich-server", "skgallery-dev_immich-machine-learning",
        "skgallery-dev_redis", "skgallery-dev_database",
    ], "skgallery.yml"),
]


def _run(tmp_path, app, services, compose_name, present):
    path = next(p for p in DEPLOY_FILES if p.parent.name == app)
    tmpl = jinja2.Environment(undefined=jinja2.ChainableUndefined).from_string(path.read_text())
    script = tmpl.render(env="dev", app=app, app_networks=[], **{app: {}})
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / compose_name).write_text("services: {}\n")
    script, n = re.subn(r"^CONFIG_DIR=.*$", f'CONFIG_DIR="{config_dir}"', script, count=1, flags=re.M)
    assert n == 1
    deploy = tmp_path / "deploy.sh"
    deploy.write_text(script)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("sleep", "#!/bin/sh\nexit 0\n")):
        f = bindir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)

    env = dict(
        os.environ,
        PATH=f"{bindir}:{os.environ['PATH']}",
        PRESENT="1" if present else "0",
        DEPLOYED=str(tmp_path / "deployed"),
        UPDATELOG=str(tmp_path / "updates"),
        SERVICES=" ".join(services),
    )
    r = subprocess.run(["bash", str(deploy)], env=env, capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert (tmp_path / "deployed").exists(), "script never reached `docker stack deploy`:\n" + out
    log = (tmp_path / "updates").read_text() if (tmp_path / "updates").exists() else ""
    forced = [l for l in log.splitlines() if "--force" in l]
    return forced, out


@pytest.mark.parametrize("app,services,compose_name", CASES, ids=[c[0] for c in CASES])
def test_first_install_skips_forced_update(tmp_path, app, services, compose_name):
    forced, out = _run(tmp_path, app, services, compose_name, present=False)
    assert forced == [], (
        "first install force-restarted services mid first-boot init:\n"
        + "\n".join(forced) + "\n---\n" + out
    )
    for svc in services:
        assert f"First install of {svc}: skipping forced restart" in out, out


@pytest.mark.parametrize("app,services,compose_name", CASES, ids=[c[0] for c in CASES])
def test_redeploy_still_forces_update(tmp_path, app, services, compose_name):
    forced, out = _run(tmp_path, app, services, compose_name, present=True)
    for svc in services:
        assert any(line.split()[-1] == svc for line in forced), (
            f"redeploy did not force-update {svc}:\n" + "\n".join(forced) + "\n---\n" + out
        )
