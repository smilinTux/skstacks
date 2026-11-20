"""shared/tasks/postgres_hba_guard.yml: an existing data dir always gets the network login rule.

The postgres entrypoint writes `host all all all scram-sha-256` only when it creates the
cluster. A hand-made data dir (manual initdb during the nor skgit restore, 2026-11-11) lacked
it and the app was refused until the line was added by hand. The guard runs before the stack
deploy, in both data-dir layouts, and never touches a fresh dir or an existing rule.
"""
import pathlib
import subprocess

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
GUARD = ANSIBLE / "shared/tasks/postgres_hba_guard.yml"
LOCAL_ONLY = "local all all trust\nhost all all 127.0.0.1/32 trust\n"
RULE = "host all all all scram-sha-256"


def _script(data_path):
    task = yaml.safe_load(GUARD.read_text())[0]
    return jinja2.Template(task["shell"]).render(pg_hba_data_path=str(data_path))


def _run(data_path):
    r = subprocess.run(["sh", "-c", _script(data_path)], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def _cluster(root, layout, hba=LOCAL_ONLY):
    d = root if layout == "flat" else root / "18" / "docker"
    d.mkdir(parents=True, exist_ok=True)
    (d / "PG_VERSION").write_text("18\n")
    (d / "pg_hba.conf").write_text(hba)
    return d / "pg_hba.conf"


def test_fresh_dir_is_left_alone(tmp_path):
    assert _run(tmp_path) == (0, "fresh")
    assert list(tmp_path.iterdir()) == []


def test_missing_dir_is_fresh(tmp_path):
    assert _run(tmp_path / "nope") == (0, "fresh")


@pytest.mark.parametrize("layout", ["flat", "nested"])
def test_rule_added_once(tmp_path, layout):
    hba = _cluster(tmp_path, layout)
    rc, out = _run(tmp_path)
    assert rc == 0 and out.startswith("added")
    assert hba.read_text().startswith(LOCAL_ONLY)
    assert hba.read_text().count(RULE) == 1
    assert _run(tmp_path) == (0, "present")
    assert hba.read_text().count(RULE) == 1


@pytest.mark.parametrize("existing", ["host all all all md5", "host  all  all  0.0.0.0/0  scram-sha-256"])
def test_existing_rule_is_not_touched(tmp_path, existing):
    hba = _cluster(tmp_path, "flat", LOCAL_ONLY + existing + "\n")
    before = hba.read_text()
    assert _run(tmp_path) == (0, "present")
    assert hba.read_text() == before


def test_commented_rule_does_not_count(tmp_path):
    hba = _cluster(tmp_path, "flat", LOCAL_ONLY + "# host all all all scram-sha-256\n")
    rc, out = _run(tmp_path)
    assert out.startswith("added")


def test_missing_hba_with_pg_version_fails(tmp_path):
    (tmp_path / "PG_VERSION").write_text("16\n")
    rc, _ = _run(tmp_path)
    assert rc == 1


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_skgit_runs_the_guard_before_the_deploy(env):
    plays = yaml.safe_load((ANSIBLE / f"optional/skgit/deploy_skgit-{env}.yml").read_text())
    play = next(p for p in plays if any(t.get("name") == "Deploy {{ app }} stack" for t in p.get("tasks", [])))
    names = [t.get("name", "") for t in play["tasks"]]
    guard = next(i for i, t in enumerate(play["tasks"])
                 if "postgres_hba_guard.yml" in str(t.get("include_tasks", "")))
    assert play["tasks"][guard]["vars"]["pg_hba_data_path"] == "/var/data/runtime/{{ app }}-{{ env }}/postgres"
    assert guard < names.index("Deploy {{ app }} stack")
