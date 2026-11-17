"""skmesh: the postgres runtime directory owner and mode follow the image.

Below 18 that directory is PGDATA itself (mounted at /var/lib/postgresql/data)
and PostgreSQL refuses a group or world readable data directory, so the deploys
create it 0700 (the same defect fixed for skgit in test_skgit_pgdata_mode.py).

From 18 on it is the PARENT of PGDATA (mounted at /var/lib/postgresql, PGDATA is
/var/lib/postgresql/18/docker). The alpine image re-execs as uid 70, which must
traverse the parent; a 999-owned 0700 parent stopped initdb with
"mkdir: can't create directory '/var/lib/postgresql/18/': Permission denied"
(skstacks-v2.26.26 gate, fixed on the release line by #436). So 18+ keeps 0755
and the entrypoint makes PGDATA itself 0700.

The owner is the uid the image's postgres runs as (70 alpine, 999 Debian). The
deploys wrote 999 for every image while the alpine entrypoint chowns PGDATA to
70 on each start, so live hosts drifted from the framework (70:0700 live vs
999:0755 rendered on an estate running 16 alpine).
"""
import pathlib
import re

import jinja2
import pytest
import yaml

SKMESH = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmesh"
PLAYBOOKS = sorted(SKMESH.glob("deploy_skmesh-*.yml"))
TEMPLATE = SKMESH / "src/config/skmesh/skmesh.yml.j2"

# (skmesh.POSTGRES_IMAGE or None for unset, expected mode, expected owner and group)
CASES = [
    (None, "0755", "70"),  # role default is postgres 18 alpine
    ("postgres:18.6-alpine@sha256:" + "0" * 64, "0755", "70"),
    ("postgres:18-alpine", "0755", "70"),
    ("docker.io/library/postgres:19.1", "0755", "999"),
    ("pgvector/pgvector:pg18", "0755", "999"),
    ("postgres:16.15-alpine@sha256:" + "1" * 64, "0700", "70"),  # a pinned pre-18 alpine image
    ("postgres:17", "0700", "999"),
    ("postgres:17.2@sha256:" + "a1" * 32, "0700", "999"),  # "alpine" only counts before the digest
]


def _play(playbook):
    plays = [p for p in yaml.safe_load(playbook.read_text()) if "skmesh_pg_major" in (p.get("vars") or {})]
    assert len(plays) == 1, f"{playbook.name}: expected one play defining skmesh_pg_major"
    return plays[0]


def _pg_dir_item(playbook):
    play = _play(playbook)
    for task in play.get("tasks", []) + play.get("pre_tasks", []):
        for item in task.get("loop", []) or task.get("with_items", []) or []:
            if (isinstance(item, dict) and "mode" in item
                    and item.get("path", "").startswith("/var/data/runtime/")
                    and item["path"].endswith("/postgres")):
                return item
    raise AssertionError(f"{playbook.name}: no postgres runtime directory item found")


def _render(expr, **ctx):
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(expr).render(**ctx)


def _major_expr(text):
    """The image-major expression, with surrounding Jinja delimiters and set/assign removed."""
    m = re.search(r"\(\(?skmesh\.POSTGRES_IMAGE \| default\('postgres:18-alpine'\)\)[^}%]*?\| int\(0\)", text)
    assert m, "image-major expression not found"
    return m.group(0).lstrip("(")


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize("image,mode,uid", CASES, ids=lambda v: str(v))
def test_postgres_runtime_dir_follows_the_image(playbook, image, mode, uid):
    play = _play(playbook)
    skmesh = {} if image is None else {"POSTGRES_IMAGE": image}
    major = _render(play["vars"]["skmesh_pg_major"], skmesh=skmesh)
    pg_uid = _render(play["vars"]["skmesh_pg_uid"], skmesh=skmesh)
    item = _pg_dir_item(playbook)
    assert _render(item["mode"], skmesh_pg_major=major) == mode, (image, major)
    assert _render(item["owner"], skmesh_pg_uid=pg_uid) == uid, image
    assert _render(item["group"], skmesh_pg_uid=pg_uid) == uid, image


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_playbook_major_matches_the_compose_template(playbook):
    """The mode and the bind-mount target must agree on the major, so the parse is shared text."""
    assert _major_expr(_play(playbook)["vars"]["skmesh_pg_major"]) == _major_expr(TEMPLATE.read_text())


@pytest.mark.parametrize("image,expected,_uid", CASES, ids=lambda v: str(v))
def test_template_mounts_pgdata_exactly_when_mode_is_0700(image, expected, _uid):
    skmesh = {} if image is None else {"POSTGRES_IMAGE": image}
    line = next(l for l in TEMPLATE.read_text().splitlines() if "set _pg_data_mount" in l)
    mount = _render(line + "{{ _pg_data_mount }}", skmesh=skmesh)
    assert mount == ("/var/lib/postgresql/data" if expected == "0700" else "/var/lib/postgresql")


def test_skmesh_playbooks_are_covered():
    assert {p.name for p in PLAYBOOKS} >= {"deploy_skmesh-prod.yml", "deploy_skmesh-dev.yml"}


def test_owner_default_matches_the_template_image_default():
    """The uid is chosen from the same default image the compose file runs."""
    text = TEMPLATE.read_text()
    for playbook in PLAYBOOKS:
        assert "default('postgres:18-alpine')" in _play(playbook)["vars"]["skmesh_pg_uid"]
    assert "image: {{ skmesh.POSTGRES_IMAGE | default('postgres:18" in text and "alpine" in text
