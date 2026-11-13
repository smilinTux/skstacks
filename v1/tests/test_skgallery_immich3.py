"""skgallery: Immich v3 default, the fail-closed major guard, and the digest-pinned stack.

Immich runs its database migrations on start and does not support downgrades, so
v2 to v3 is a one-way step on an instance's database. The framework default moved
to v3.4.1 (server and machine learning, each pinned by its own index digest) behind:

  * tasks/immich_major_guard.yml, run by every playbook before the stack deploy: it
    compares the running server image with the one about to be deployed and refuses an
    unacknowledged major upgrade, a downgrade, an unreadable running state, pgvecto.rs
    on v3 (removed upstream) and a machine-learning image of another major;
  * the knobs that keep an instance where it is: IMMICH_VERSION pins both images,
    IMMICH_ML_VERSION pins machine learning alone, IMMICH_MAJOR_ACK acknowledges one
    upgrade, and a v2 pin keeps rendering and deploying;
  * the Immich database image stays on Postgres 14 (Immich's own compose still ships it),
    with the shared Postgres major guard in front of the deploy and a mount target that
    follows the image major; renovate.json stops proposing a Postgres major for the role.
"""
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import textwrap

import pytest
import yaml

from image_pins import is_pinned, template_default
from test_router_middlewares_knob import _env, render

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / "v1/ansible/optional/skgallery"
CFG = APP / "src/config/skgallery"
GUARD = APP / "tasks/immich_major_guard.yml"
PG_GUARD = ROOT / "v1/ansible/shared/tasks/postgres_major_guard.yml"
ENVS = ("dev", "staging", "prod")
SERVER = "ghcr.io/immich-app/immich-server"
ML = "ghcr.io/immich-app/immich-machine-learning"
DIGEST = "a" * 64
needs_ansible = pytest.mark.skipif(not shutil.which("ansible-playbook"), reason="ansible not installed")


def _svc(name, env="prod", **over):
    return yaml.safe_load(render("skgallery", env, **over))["services"][name]


def _tag(image):
    return image.split("@")[0].split(":")[-1]


# ---- defaults ---------------------------------------------------------------------------

@pytest.mark.parametrize("env", ENVS)
def test_default_is_one_v3_release_for_server_and_machine_learning_each_digest_pinned(env):
    srv, ml = _svc("immich-server", env)["image"], _svc("immich-machine-learning", env)["image"]
    assert is_pinned(srv, SERVER, "v3.") and is_pinned(ml, ML, "v3.")
    assert _tag(srv) == _tag(ml)
    # two different images, two different index digests
    assert srv.split("@")[1] != ml.split("@")[1]


def test_split_workers_microservices_runs_the_same_server_image():
    srv = _svc("immich-server", SPLIT_WORKERS=True)["image"]
    assert _svc("immich-microservices", SPLIT_WORKERS=True)["image"] == srv


def test_every_default_image_is_pinned_by_tag_and_digest():
    for name in ("immich-server", "immich-machine-learning", "database", "redis", "postgres-db-backup"):
        img = _svc(name)["image"]
        assert re.search(r":[\w.-]+@sha256:[0-9a-f]{64}$", img), (name, img)
        assert "latest" not in img.split("@")[0].split(":")[-1] and ":release" not in img


def test_redis_and_dump_sidecar_bumps_from_the_grouped_renovate_pr():
    assert _svc("redis")["image"].startswith("redis:8.10.3-alpine@sha256:7958cfbb")
    assert _svc("postgres-db-backup")["image"].startswith("postgres:14.25@sha256:9c8a7df7")


def test_database_image_stays_on_postgres_14_and_the_sidecar_matches_its_major():
    db, bk = _svc("database")["image"], _svc("postgres-db-backup")["image"]
    assert is_pinned(db, "ghcr.io/immich-app/postgres", "14-vectorchord")
    assert re.match(r"postgres:14[.@]", bk), bk
    assert "/var/lib/postgresql/data" in _svc("database")["volumes"][0]


# ---- pins and the way back --------------------------------------------------------------

def test_a_plain_tag_pin_pins_both_images_and_drops_the_digest():
    """An instance whose vault already pins v3.3.5 keeps it: server and ML both follow IMMICH_VERSION."""
    assert _svc("immich-server", IMMICH_VERSION="v3.3.5")["image"] == f"{SERVER}:v3.3.5"
    assert _svc("immich-machine-learning", IMMICH_VERSION="v3.3.5")["image"] == f"{ML}:v3.3.5"
    assert _svc("immich-microservices", IMMICH_VERSION="v3.3.5", SPLIT_WORKERS=True)["image"] == f"{SERVER}:v3.3.5"


def test_a_v2_pin_keeps_rendering_both_images():
    assert _svc("immich-server", IMMICH_VERSION="v2.7.5")["image"] == f"{SERVER}:v2.7.5"
    assert _svc("immich-machine-learning", IMMICH_VERSION="v2.7.5")["image"] == f"{ML}:v2.7.5"


def test_ml_version_knob_overrides_machine_learning_alone_and_wins_over_immich_version():
    s = lambda **o: _svc("immich-machine-learning", **o)["image"]
    assert s(IMMICH_ML_VERSION="v3.3.5") == f"{ML}:v3.3.5"
    assert s(IMMICH_VERSION="v3.2.0", IMMICH_ML_VERSION="v3.3.5") == f"{ML}:v3.3.5"
    assert _svc("immich-server", IMMICH_ML_VERSION="v3.3.5")["image"].startswith(f"{SERVER}:v3.4.1@sha256:")


def test_database_version_pin_and_mount_target_follow_the_image_major():
    assert _svc("database", POSTGRES_VERSION="14-vectorchord0.4.3")["image"].endswith("postgres:14-vectorchord0.4.3")
    assert _svc("database", POSTGRES_VERSION="17-vectorchord1.1.1")["volumes"][0].endswith(":/var/lib/postgresql/data")
    new = _svc("database", POSTGRES_VERSION="18-vectorchord1.1.1@sha256:" + DIGEST)
    assert new["volumes"][0] == "/var/data/skgallery-prod/db-data:/var/lib/postgresql"


def test_env_files_carry_the_tag_only():
    for name, pin, want in (("skgallery.env.j2", None, "v3.4.1"), ("skgallery.sh.env.j2", None, "v3.4.1"),
                            ("skgallery.env.j2", f"v3.3.5@sha256:{DIGEST}", "v3.3.5"),
                            ("skgallery.sh.env.j2", "v2.7.5", "v2.7.5")):
        ctx = {"DB_PASSWORD": "x"} | ({"IMMICH_VERSION": pin} if pin else {})
        out = _env().from_string((CFG / name).read_text()).render(
            app="skgallery", env="prod", domain="example.test", cluster_name="c", skgallery=ctx)
        assert re.search(rf"^IMMICH_VERSION={re.escape(want)}$", out, re.M), (name, pin)


def test_template_literals_agree_on_the_release():
    """Renovate edits the image lines only; the env files and the playbook guards carry the release by hand."""
    yml = (CFG / "skgallery.yml.j2").read_text()
    version = _tag(template_default(yml, "IMMICH_VERSION"))
    for f in ("skgallery.env.j2", "skgallery.sh.env.j2"):
        assert f"default('{version}')" in (CFG / f).read_text(), f
    ml = re.search(r"_ml_pin \| default\('([^']+)'\)", yml).group(1)
    assert _tag(ml) == version


# ---- playbooks --------------------------------------------------------------------------

@pytest.mark.parametrize("env", ENVS)
def test_every_playbook_runs_both_guards_before_the_stack_deploy_once(env):
    text = (APP / f"deploy_skgallery-{env}.yml").read_text()
    deploy = text.index("- name: Deploy {{ app }} stack")
    for needle in ("../../shared/tasks/postgres_major_guard.yml", "tasks/immich_major_guard.yml"):
        assert text.count(needle) == 1, needle
        assert text.index(needle) < deploy, needle
    assert "immich_guard_ack: \"{{ skgallery.IMMICH_MAJOR_ACK" in text


def test_playbook_guard_defaults_agree_with_the_template():
    yml = (CFG / "skgallery.yml.j2").read_text()
    template = template_default(yml, "IMMICH_VERSION")
    pg = template_default(yml, "POSTGRES_VERSION")
    major = lambda ref: re.match(r"v?(\d+)", ref.split("@")[0]).group(1)
    for env in ENVS:
        text = (APP / f"deploy_skgallery-{env}.yml").read_text()
        guard = re.search(r"immich-server:\{\{ skgallery\.IMMICH_VERSION \| default\('([^']+)'\)", text).group(1)
        assert major(guard) == major(template), env
        pg_guard = re.search(r"immich-app/postgres:\{\{ skgallery\.POSTGRES_VERSION \| default\('([^']+)'\)", text).group(1)
        assert major(pg_guard) == major(pg), env
        assert "pg_guard_node: \"{{ skgallery.DATA_NODE" in text


def test_renovate_does_not_propose_a_postgres_major_for_the_role():
    cfg = json.loads((ROOT / "renovate.json").read_text())
    rules = [r for r in cfg["packageRules"]
             if "v1/ansible/optional/skgallery/**" in r.get("matchFileNames", [])]
    assert len(rules) == 1
    r = rules[0]
    assert r["matchPackageNames"] == ["postgres"] and r["matchUpdateTypes"] == ["major"] and r["enabled"] is False
    assert "Immich" in r["description"] and "14" in r["description"]


def test_renovate_still_tracks_the_immich_server_and_ml_lines():
    """Both image lines must stay inside a shape the custom manager's regexes read."""
    cfg = json.loads((ROOT / "renovate.json").read_text())
    # Renovate uses JS named groups (?<n>...); Python spells them (?P<n>...)
    pats = [re.compile(m.replace("(?<", "(?P<")) for m in cfg["customManagers"][0]["matchStrings"]]
    names = set()
    for line in (CFG / "skgallery.yml.j2").read_text().splitlines():
        if "image:" in line and "immich-app/immich" in line:
            ms = [m for p in pats if (m := p.search(line))]
            assert ms, line
            assert ms[0].group("currentDigest").startswith("sha256:"), line
            names.add(ms[0].group("depName"))
    assert names == {SERVER, ML}


# ---- the guard, run for real -------------------------------------------------------------

def _run(tmp_path, tasks_file=GUARD, path=None, **extra):
    pb = tmp_path / "pb.yml"
    pb.write_text(textwrap.dedent(f"""
        - hosts: localhost
          connection: local
          gather_facts: false
          tasks:
            - import_tasks: {tasks_file}
    """))
    ev = tmp_path / "vars.json"
    ev.write_text(json.dumps(extra))
    env = {"PATH": path or os.environ["PATH"], "HOME": str(tmp_path), "ANSIBLE_LOCAL_TEMP": str(tmp_path / "t")}
    return subprocess.run(["ansible-playbook", str(pb), "-e", f"@{ev}"], capture_output=True, text=True,
                          cwd=tmp_path, env=env)


def _guard(tmp_path, running, target, ack="", ext="vectorchord", ml=""):
    return _run(tmp_path, immich_guard_stack="demo-prod", immich_guard_running_image=running,
                immich_guard_target_image=target, immich_guard_ack=ack, immich_guard_vector_ext=ext,
                immich_guard_ml_image=ml)


S = lambda tag: f"{SERVER}:{tag}"
RUN2, RUN3 = S("v2.7.5@sha256:" + DIGEST), S("v3.3.5")


@needs_ansible
@pytest.mark.parametrize(
    "running,target,ack,ok",
    [
        ("", S("v3.4.1@sha256:" + DIGEST), "", True),     # first install: nothing to guard
        (RUN3, S("v3.4.1@sha256:" + DIGEST), "", True),   # the framework default moving within v3
        (RUN3, S("v3.3.5"), "", True),                    # same release, redeploy
        (RUN2, S("v2.7.5"), "", True),                    # an instance pinned to its v2
        (RUN2, S("v3.4.1@sha256:" + DIGEST), "", False),  # the upgrade that must not run unattended
        (RUN2, S("v3.4.1@sha256:" + DIGEST), "3", True),
        (RUN2, S("v3.4.1@sha256:" + DIGEST), "v3", True),
        (RUN2, S("v3.4.1"), 3, True),                     # the ack as a YAML number
        (RUN2, S("v3.4.1"), "2", False),                  # a stale ack for another major
        (RUN2, S("v3"), "", False),                       # the v3 metatag, still a major step
        (RUN3, S("v2.7.5"), "3", False),                  # downgrade across a major, even with an ack
        (RUN3, S("v2.7.5"), "", False),
        (RUN3, S("release"), "", False),                  # floating tag over a running stack
        ("", S("release"), "", True),                     # floating tag on a first install: not decidable, not a major jump
        ("registry.example.test/mirror/other:latest", S("v3.4.1"), "", False),   # unreadable running major
        ("registry.example.test/mirror/other:latest", S("v3.4.1"), "3", True),
    ],
)
def test_major_guard_behaviour(tmp_path, running, target, ack, ok):
    r = _guard(tmp_path, running, target, ack)
    assert (r.returncode == 0) == ok, r.stdout + r.stderr
    if not ok:
        out = r.stdout.replace("\n", " ")
        assert "nothing was changed" in out
        assert "skgallery.IMMICH_VERSION" in out


@needs_ansible
def test_refusal_names_the_ack_knob_the_backup_and_the_way_to_stay(tmp_path):
    out = _guard(tmp_path, RUN2, S("v3.4.1")).stdout.replace("\n", " ")
    for needle in ('skgallery.IMMICH_MAJOR_ACK to "3"', "back up the database", "upgrade the mobile apps",
                   "immich.app/blog/v3-migration", "pin skgallery.IMMICH_VERSION to your current release"):
        assert needle in out, needle


@needs_ansible
@pytest.mark.parametrize(
    "running,target,ext,ok",
    [
        ("", S("v3.4.1"), "pgvecto.rs", False),           # removed in v3, even on a first install
        (RUN3, S("v3.4.1"), "pgvecto.rs", False),
        (RUN2, S("v3.4.1"), "PGVECTO.RS", False),
        ("", S("v3.4.1"), "vectorchord", True),
        ("", S("v3.4.1"), "pgvector", True),
        (RUN2, S("v2.7.5"), "pgvecto.rs", True),          # a v2 pin keeps what it has
        ("", S("v2.7.5"), "pgvecto.rs", True),
    ],
)
def test_pgvecto_rs_is_refused_for_v3_only(tmp_path, running, target, ext, ok):
    r = _guard(tmp_path, running, target, ack="3", ext=ext)
    assert (r.returncode == 0) == ok, r.stdout + r.stderr
    if not ok:
        assert "removed pgvecto.rs" in r.stdout.replace("\n", " ")


@needs_ansible
@pytest.mark.parametrize(
    "ml,ok",
    [("", True), (f"{ML}:v3.3.5", True), (f"{ML}:v3.4.1@sha256:{DIGEST}", True), (f"{ML}:v2.7.5", False), (f"{ML}:release", False)],
)
def test_machine_learning_pin_must_be_the_servers_major(tmp_path, ml, ok):
    r = _guard(tmp_path, RUN3, S("v3.4.1"), ml=ml)
    assert (r.returncode == 0) == ok, r.stdout + r.stderr
    if not ok:
        assert "same Immich version" in r.stdout.replace("\n", " ")


def _fake_docker(tmp_path, body):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    d = bindir / "docker"
    d.write_text("#!/bin/sh\n" + body)
    d.chmod(d.stat().st_mode | stat.S_IEXEC)
    return f"{bindir}:{os.environ['PATH']}"


def _probe(tmp_path, docker_body, target=None, ack=""):
    return _run(tmp_path, path=_fake_docker(tmp_path, docker_body), immich_guard_stack="demo-prod",
                immich_guard_target_image=target or S("v3.4.1"), immich_guard_ack=ack,
                immich_guard_vector_ext="vectorchord")


@needs_ansible
def test_probe_reads_the_running_service_image_and_asks_for_the_stack_service(tmp_path):
    log = tmp_path / "docker.log"
    path = _fake_docker(tmp_path, f'echo "$@" >> {log}\necho "{RUN2}"\n')
    r = _run(tmp_path, path=path, immich_guard_stack="demo-prod", immich_guard_target_image=S("v3.4.1"),
             immich_guard_ack="", immich_guard_vector_ext="vectorchord")
    assert r.returncode != 0 and "Immich major 2" in r.stdout.replace("\n", " ")
    assert "service inspect" in log.read_text() and "demo-prod_immich-server" in log.read_text()
    assert _probe(tmp_path, f'echo "{RUN2}"\n', ack="3").returncode == 0


@needs_ansible
def test_probe_no_such_service_is_a_first_install(tmp_path):
    r = _probe(tmp_path, 'echo "Status: Error: no such service: x, Code: 1" >&2\nexit 1\n')
    assert r.returncode == 0, r.stdout + r.stderr


@needs_ansible
@pytest.mark.parametrize("err", ["Cannot connect to the Docker daemon", "Error response from daemon: This node is not a swarm manager"])
def test_probe_failing_for_any_other_reason_stops_the_play(tmp_path, err):
    """An unreadable state is not a pass: a stopped daemon must not read as a first install."""
    r = _probe(tmp_path, f'echo "{err}" >&2\nexit 1\n', ack="3")
    assert r.returncode != 0, r.stdout + r.stderr


# ---- the shared Postgres guard as the skgallery playbooks call it -------------------------

@needs_ansible
@pytest.mark.parametrize(
    "disk,tag,ok",
    [
        (None, "14-vectorchord0.4.3-pgvectors0.2.0@sha256:" + DIGEST, True),   # first install
        (("flat", 14), "14-vectorchord0.4.3-pgvectors0.2.0@sha256:" + DIGEST, True),   # redeploy
        (("flat", 14), "18-vectorchord1.1.1", False),     # a pinned 18 over a 14 data directory
        (("flat", 14), "17-vectorchord1.1.1", False),
        (("nested", 18), "14-vectorchord0.4.3-pgvectors0.2.0", False),   # rollback onto 18 data
    ],
)
def test_postgres_guard_with_the_immich_database_image(tmp_path, disk, tag, ok):
    data = tmp_path / "pgdata"
    if disk:
        layout, major = disk
        target = data if layout == "flat" else data / str(major) / "docker"
        target.mkdir(parents=True)
        (target / "PG_VERSION").write_text(f"{major}\n")
    r = _run(tmp_path, PG_GUARD, pg_guard_app="skgallery", pg_guard_image=f"ghcr.io/immich-app/postgres:{tag}",
             pg_guard_data_path=str(data), pg_guard_pin_var="skgallery.POSTGRES_VERSION")
    assert (r.returncode == 0) == ok, r.stdout + r.stderr
    if not ok:
        assert "skgallery.POSTGRES_VERSION" in r.stdout.replace("\n", " ")
