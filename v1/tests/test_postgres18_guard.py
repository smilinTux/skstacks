"""Postgres 18 for skgit, skmesh, sksso and skorch: the data-dir layout and the fail-closed guard.

Postgres 18 images moved PGDATA to /var/lib/postgresql/<major>/docker and declare VOLUME
/var/lib/postgresql, and no major can open another's data directory. So:

  * the compose templates mount the data directory at /var/lib/postgresql for an 18+ image
    and keep /var/lib/postgresql/data for a pinned older image (vault knob POSTGRES_IMAGE);
  * shared/tasks/postgres_major_guard.yml reads PG_VERSION (either layout) before the stack
    deploy and refuses a mismatch, because an 18 image beside an old data directory would
    initdb an empty database instead of failing;
  * skmem-pg stays on 17 (its image is built from our own Dockerfile and no 18 build is published).
"""
import os
import pathlib
import re
import shutil
import subprocess
import textwrap

import pytest
import yaml

import test_router_middlewares_knob as rm
import test_skmesh_turn_relay_modes as mesh
import test_sksso_local_db as sso
from image_pins import is_pinned

OPTIONAL = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional"
GUARD = OPTIONAL.parent / "shared/tasks/postgres_major_guard.yml"
ENVS = ("dev", "staging", "prod")
MOUNT18, MOUNT_OLD = "/var/lib/postgresql", "/var/lib/postgresql/data"
PIN16 = "postgres:16.15@sha256:" + "a" * 64


def _render(role, **over):
    """Services of the rendered compose for a role, with vault overrides."""
    if role in ("skgit", "skorch"):
        return yaml.safe_load(rm.render(role, **over))["services"]
    if role == "sksso":
        return sso.services(**over)
    return mesh._compose(dict(mesh.BASE, **over))["services"]


def _pg_mount(svc):
    return [v.split(":", 1)[1].split(":")[0] for v in svc["volumes"]
            if isinstance(v, str) and "/postgres" in v.split(":")[0] and "/runtime/" in v.split(":")[0]
            or isinstance(v, str) and v.startswith("/var/data/runtime/sksso-prod/postgres:")]


# image knob per role (server) and (backup service name, backup knob)
ROLES = {
    "skgit": ("POSTGRES_IMAGE", "db-backup", "POSTGRES_BACKUP_IMAGE"),
    "skmesh": ("POSTGRES_IMAGE", "postgres-db-backup", "POSTGRES_BACKUP_IMAGE"),
    "sksso": ("POSTGRES_IMAGE", "postgres-db-backup", "POSTGRES_BACKUP_IMAGE"),
    "skorch": ("POSTGRES_IMAGE", "postgres-backup", "POSTGRES_BACKUP_IMAGE"),
}


@pytest.mark.parametrize("role", ROLES)
def test_default_is_postgres_18_on_the_parent_mount(role):
    s = _render(role)
    img = s["postgres"]["image"]
    assert re.search(r":(pg)?18[.\-@]|:(pg)?18$", img), img
    assert "@sha256:" in img
    mounts = [v for v in s["postgres"]["volumes"] if v.startswith("/var/data/runtime/")]
    assert mounts and all(m.endswith(f"/postgres:{MOUNT18}") for m in mounts), mounts
    assert not any(v.endswith(MOUNT_OLD) for v in s["postgres"]["volumes"])


@pytest.mark.parametrize("role", ROLES)
def test_backup_image_major_matches_the_server(role):
    s = _render(role)
    _, bsvc, _ = ROLES[role]
    assert re.search(r":18[.\-@]", s[bsvc]["image"]) or ":18-" in s[bsvc]["image"], s[bsvc]["image"]


def test_skorch_backup_image_is_pinned_18():
    img = _render("skorch")["postgres-backup"]["image"]
    assert is_pinned(img, "smilintux/postgres-backup-local", "18-")


@pytest.mark.parametrize("role", ROLES)
def test_pinning_an_old_image_keeps_the_old_mount_target(role):
    knob, bsvc, bknob = ROLES[role]
    pin = "pgvector/pgvector:pg16@sha256:" + "b" * 64 if role == "skorch" else PIN16
    s = _render(role, **{knob: pin, bknob: PIN16})
    assert s["postgres"]["image"] == pin
    assert s[bsvc]["image"] == PIN16
    mounts = [v for v in s["postgres"]["volumes"] if v.startswith("/var/data/runtime/")]
    assert mounts and all(m.endswith(f"/postgres:{MOUNT_OLD}") for m in mounts), mounts


@pytest.mark.parametrize("pin,expect", [
    ("postgres:16.15-alpine", MOUNT_OLD), ("postgres:17", MOUNT_OLD), ("postgres:18-alpine", MOUNT18),
    ("registry.example.test:5000/postgres:18.6@sha256:" + "c" * 64, MOUNT18), ("pgvector/pgvector:pg17", MOUNT_OLD),
    ("pgvector/pgvector:pg18", MOUNT18), ("postgres:19", MOUNT18),
])
def test_mount_target_follows_the_image_major(pin, expect):
    s = _render("skgit", POSTGRES_IMAGE=pin)
    assert s["postgres"]["volumes"][0].endswith(f"/postgres:{expect}")


def test_sksso_custom_data_path_follows_the_same_rule():
    s = sso.services(**sso.LOCAL)
    assert "/var/lib/sksso/postgres:" + MOUNT18 in s["postgres"]["volumes"]
    s = sso.services(**sso.LOCAL, POSTGRES_IMAGE=PIN16)
    assert "/var/lib/sksso/postgres:" + MOUNT_OLD in s["postgres"]["volumes"]


def _template_major(role):
    t = {"skgit": "skgit", "skmesh": "skmesh", "sksso": "sksso", "skorch": "skorch"}[role]
    text = (OPTIONAL / role / f"src/config/{t}/{t}.yml.j2").read_text()
    img = re.search(r"image: \{\{ \w+\.POSTGRES_IMAGE \| default\('([^']+)'\)", text).group(1)
    return img, re.search(r"_pg_data_mount = .*?POSTGRES_IMAGE \| default\('([^']+)'\)", text).group(1)


@pytest.mark.parametrize("role", ROLES)
def test_default_majors_agree_across_template_mount_guard_and_playbooks(role):
    """Bumping a default to another major must be a deliberate change in all four places."""
    major = lambda ref: re.search(r"(\d+)", ref.split("@")[0].split("/")[-1].split(":")[1]).group(1)
    img, mount_default = _template_major(role)
    assert major(img) == major(mount_default)
    for env in ENVS:
        pb = OPTIONAL / role / f"deploy_{role}-{env}.yml"
        if not pb.exists():
            continue
        text = pb.read_text()
        guard_default = re.search(r"pg_guard_image: \"\{\{ \w+\.POSTGRES_IMAGE \| default\('([^']+)'\) \}\}\"", text).group(1)
        assert major(guard_default) == major(img), pb.name


@pytest.mark.parametrize("role", ROLES)
def test_every_playbook_runs_the_guard_before_the_stack_deploy(role):
    pbs = sorted((OPTIONAL / role).glob("deploy_*.yml"))
    assert pbs
    for pb in pbs:
        text = pb.read_text()
        guard = text.index("../../shared/tasks/postgres_major_guard.yml")
        deploy = text.index("- name: Deploy {{ app }} stack")
        assert guard < deploy, pb.name
        assert text.count("postgres_major_guard.yml") == 1


def test_skmem_pg_stays_on_17():
    text = (OPTIONAL / "skmem-pg/src/config/skmem-pg/skmem-pg.yml.j2").read_text()
    assert "smilintux/skmem-pg:pg17-bm25-age" in text
    assert re.search(r"image: postgres:17\.", text)
    assert "/var/lib/postgresql/data" in text


def test_v2_authentik_postgres_keeps_a_stable_data_path():
    """v2 has no guard task: keep PGDATA on the mounted path so an old volume fails in Postgres itself."""
    c = yaml.safe_load((OPTIONAL.parents[2] / "v2/core/sksso/swarm-compose.yml").read_text())["services"]["postgresql"]
    assert c["image"].startswith("postgres:18")
    assert "PGDATA=/var/lib/postgresql/data" in c["environment"]
    assert "authentik-db:/var/lib/postgresql/data" in c["volumes"]


# ---- the guard itself, run for real -----------------------------------------------------

@pytest.mark.skipif(not shutil.which("ansible-playbook"), reason="ansible not installed")
@pytest.mark.parametrize(
    "disk,image,ok",
    [
        (None, "postgres:18.6", True),                  # no directory: fresh install
        ("empty", "postgres:18.6", True),               # empty directory: fresh install
        (("nested", 18), "postgres:18.6@sha256:" + "a" * 64, True),   # redeploy, same major
        (("nested", 18), "pgvector/pgvector:pg18", True),
        (("flat", 16), "postgres:18.6", False),         # the upgrade that must not run unattended
        (("flat", 17), "postgres:18.6-alpine", False),
        (("flat", 16), "pgvector/pgvector:pg18", False),
        (("flat", 16), "postgres:16.15", True),         # instance pinned to its current major
        (("flat", 16), "postgres:17.7", False),         # a skipped or sideways pre-18 major
        (("nested", 18), "postgres:16.15", False),      # rollback onto 18 data
        (("nested", 18), "postgres:19.0", False),
        (("flat", 16), "postgres:latest", True),        # no major in the tag: not checked
    ],
)
def test_guard_behaviour(tmp_path, disk, image, ok):
    data = tmp_path / "pgdata"
    if disk is not None:
        data.mkdir()
        if disk != "empty":
            layout, major = disk
            target = data if layout == "flat" else data / str(major) / "docker"
            target.mkdir(parents=True, exist_ok=True)
            (target / "PG_VERSION").write_text(f"{major}\n")
    pb = tmp_path / "pb.yml"
    pb.write_text(textwrap.dedent(f"""
        - hosts: localhost
          connection: local
          gather_facts: false
          tasks:
            - import_tasks: {GUARD}
              vars:
                pg_guard_app: demo
                pg_guard_image: "{image}"
                pg_guard_data_path: "{data}"
                pg_guard_pin_var: demo.POSTGRES_IMAGE
    """))
    r = subprocess.run(["ansible-playbook", str(pb)], capture_output=True, text=True, cwd=tmp_path,
                       env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
                            "ANSIBLE_LOCAL_TEMP": str(tmp_path / "t")})
    assert (r.returncode == 0) == ok, r.stdout + r.stderr
    if not ok:
        out = r.stdout.replace("\n", " ")
        assert "demo.POSTGRES_IMAGE" in out and "dump-and-restore runbook" in out
        assert "nothing was changed" in out


# ---- skflow and skbom: default stays on 17, guard and layout ready for an 18 pin ----------
#
# Both roles keep postgres:17.12-alpine as the default (a shared literal, so they share a layer).
# The template computes the mount target from the playbook's effective image (sf_pg_image /
# skb_pg_image = POSTGRES_IMAGE or the role default), the same value the guard include checks.

import json  # noqa: E402

import jinja2  # noqa: E402

PG17 = "postgres:17.12-alpine@sha256:f9cf3e1f0066f0eb0c48707d864f0ac5941e9ff454a072077d97a4d278d07d73"
PIN18 = "postgres:18.6-alpine@sha256:" + "d" * 64
# role -> (image fact, path fact, playbook default fact, vars needed to render)
PG17_ROLES = {
    "skflow": ("sf_pg_image", "sf_pg_path", "sf_default_pg_image",
               {"sf_external_db": False, "sf_data_node": "", "sf_dump_path": "/var/data/skflow-prod/database-dump",
                "sf_pg_path": "/var/data/runtime/skflow-prod/postgres"}),
    "skbom": ("skb_pg_image", "skb_pg_path", "skb_default_pg_image",
              {"skb_data_node": "", "skb_dump_path": "/var/data/skbom-prod/database-dump",
               "skb_pg_path": "/var/data/runtime/skbom-prod/postgres",
               "skb_apiserver_data_path": "/var/data/skbom-prod/apiserver-data"}),
}


def _render_text(role, text, image=None):
    fact, _, _, extra = PG17_ROLES[role]
    j = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    j.filters["to_json"] = json.dumps
    j.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    ctx = dict(extra, app=role, env="prod", **{role: {"DB_PASSWORD": "x" * 16}})
    if image is not None:
        ctx[role]["POSTGRES_IMAGE"] = image
    ctx[fact] = image or PG17
    return j.from_string(text).render(**ctx)


def _template(role):
    return OPTIONAL / role / f"src/config/{role}/{role}.yml.j2"


def _pg_volumes(role, image=None):
    out = yaml.safe_load(_render_text(role, _template(role).read_text(), image))
    return [v for v in out["services"]["postgres"]["volumes"] if "/postgres:" in v]


@pytest.mark.parametrize("role", PG17_ROLES)
def test_pg17_roles_default_keeps_the_old_mount(role):
    _, path_fact, _, extra = PG17_ROLES[role]
    assert _pg_volumes(role) == [f"{extra[path_fact]}:{MOUNT_OLD}"]


@pytest.mark.parametrize("role", PG17_ROLES)
def test_pg17_roles_18_pin_mounts_the_parent(role):
    _, path_fact, _, extra = PG17_ROLES[role]
    assert _pg_volumes(role, PIN18) == [f"{extra[path_fact]}:{MOUNT18}"]
    assert _pg_volumes(role, "registry.example.test/postgres:18.6@sha256:" + "e" * 64) == [f"{extra[path_fact]}:{MOUNT18}"]
    assert _pg_volumes(role, PIN16) == [f"{extra[path_fact]}:{MOUNT_OLD}"]


@pytest.mark.parametrize("role", PG17_ROLES)
def test_pg17_roles_default_render_is_unchanged_from_before_the_mount_knob(role):
    """The default render must be byte-identical to the template with the literal old mount."""
    text = _template(role).read_text()
    _, path_fact, _, _ = PG17_ROLES[role]
    before = text.replace("{{ " + path_fact + " }}:{{ _pg_data_mount }}", "{{ " + path_fact + " }}:" + MOUNT_OLD)
    before = re.sub(r"\{# Postgres 18\+ images moved PGDATA.*?#\}\n\{% set _pg_data_mount = [^\n]*%\}\n", "", before,
                    flags=re.S)
    assert before != text and "_pg_data_mount" not in before
    assert _render_text(role, text) == _render_text(role, before)


@pytest.mark.parametrize("role", PG17_ROLES)
def test_pg17_roles_default_is_17_in_template_and_every_playbook(role):
    _, _, default_fact, _ = PG17_ROLES[role]
    assert f"default('{PG17}', true)" in _template(role).read_text()
    for env in ENVS:
        text = (OPTIONAL / role / f"deploy_{role}-{env}.yml").read_text()
        assert f'{default_fact}: "{PG17}"' in text, env


@pytest.mark.parametrize("role", PG17_ROLES)
@pytest.mark.parametrize("env", ENVS)
def test_pg17_roles_playbooks_run_the_guard_before_the_stack_deploy(role, env):
    fact, path_fact, _, _ = PG17_ROLES[role]
    pb = OPTIONAL / role / f"deploy_{role}-{env}.yml"
    text = pb.read_text()
    assert text.count("postgres_major_guard.yml") == 1
    assert text.index("../../shared/tasks/postgres_major_guard.yml") < text.index("- name: Deploy {{ app }} stack")
    task = next(t for play in yaml.safe_load(text) for t in play.get("tasks", [])
                if "postgres_major_guard.yml" in str(t.get("include_tasks", "")))
    v = task["vars"]
    assert v["pg_guard_app"] == role
    assert v["pg_guard_pin_var"] == f"{role}.POSTGRES_IMAGE"
    assert v["pg_guard_image"] == "{{ " + fact + " }}"
    assert v["pg_guard_data_path"] == "{{ " + path_fact + " }}"
    assert "DATA_NODE" not in v["pg_guard_node"] and "_data_node" in v["pg_guard_node"]
    if role == "skflow":
        assert task["when"] == "not (sf_external_db | bool)"


@pytest.mark.parametrize("role", PG17_ROLES)
def test_pg17_roles_readme_has_the_runbook_the_guard_names(role):
    text = (OPTIONAL / role / "README.md").read_text()
    assert "## Upgrading Postgres to 18" in text and "### Runbook: dump and restore (17 to 18)" in text
    assert "postgres_major_guard.yml" in text
