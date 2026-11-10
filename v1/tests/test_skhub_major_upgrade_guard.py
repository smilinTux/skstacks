"""skhub Nextcloud major upgrades: the pre-deploy guard and the post-upgrade tasks.

The official image only upgrades one major at a time and refuses a downgrade; either way
the services crash-loop. tasks/major_upgrade_guard.yml fails the play before the stack
deploy instead (leaving the running stack alone), and tasks/post_upgrade.yml finishes an
upgrade (occ upgrade safety net, app:update after a major, missing columns and keys).
"""
import pathlib
import re
import shutil
import subprocess
import textwrap

import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
GUARD = SKHUB / "tasks/major_upgrade_guard.yml"
POST = SKHUB / "tasks/post_upgrade.yml"
COMPOSE = SKHUB / "src/config/skhub/skhub.yml.j2"
PLAYBOOKS = [SKHUB / f"deploy_skhub-{e}.yml" for e in ("dev", "staging", "prod")]


def _names(path):
    text = path.read_text()
    return text, [m for m in re.findall(r"^\s+- name: (.+)$", text, re.M)]


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.stem)
def test_playbook_wires_guard_before_deploy_and_post_upgrade_after_install_wait(pb):
    text = pb.read_text()
    guard = text.index("import_tasks: tasks/major_upgrade_guard.yml")
    deploy = text.index("- name: Deploy {{ app }} stack")
    wait = text.index("- name: Wait for Nextcloud to finish its first-run install")
    post = text.index("import_tasks: tasks/post_upgrade.yml")
    redis = text.index("- name: Configure Redis in Nextcloud config.php")
    assert guard < deploy
    assert wait < post < redis


def test_guard_default_major_matches_compose_default():
    compose_major = re.search(r"NEXTCLOUD_IMAGE \| default\('nextcloud:(\d+)\.", COMPOSE.read_text()).group(1)
    guard_major = re.search(r"NEXTCLOUD_IMAGE \| default\('nextcloud:(\d+)\.", GUARD.read_text()).group(1)
    assert compose_major == guard_major


def test_post_upgrade_is_guarded_and_idempotent():
    tasks = yaml.safe_load(POST.read_text())
    by_name = {t["name"]: t for t in tasks}
    up = by_name["Run occ upgrade if the entrypoint left a database upgrade pending"]
    assert "needsDbUpgrade" in up["shell"]
    assert "SKHUB_OCC_UPGRADE_RAN" in up["changed_when"]
    assert "ignore_errors" not in up, "a failed occ upgrade leaves a half-migrated DB and must fail the play"
    apps = by_name["Update apps after a Nextcloud major upgrade"]
    assert "skhub_nc_upgrade_pending" in str(apps["when"])
    assert "occ app:update --all" in apps["shell"]
    schema = by_name["Add database columns and primary keys a Nextcloud upgrade may need"]
    assert "db:add-missing-columns" in schema["shell"] and "db:add-missing-primary-keys" in schema["shell"]
    for t in tasks:
        assert "nextcloud_container.stdout" in str(t["when"])


@pytest.mark.skipif(not shutil.which("ansible-playbook"), reason="ansible not installed")
@pytest.mark.parametrize(
    "installed,image,ok,pending",
    [
        (None, "nextcloud:35.0.2", True, False),      # fresh install
        (34, "nextcloud:35.0.2@sha256:" + "a" * 64, True, True),  # the one supported step
        (35, "nextcloud:35.0.2", True, False),        # redeploy, same major
        (31, "nextcloud:35.0.2", False, None),        # skipped majors
        (35, "nextcloud:34.0.5", False, None),        # downgrade
        (31, "nextcloud:latest", True, False),        # no major in the tag: not checked
    ],
)
def test_guard_behaviour(tmp_path, installed, image, ok, pending):
    (tmp_path / "html").mkdir()
    if installed is not None:
        (tmp_path / "html/version.php").write_text(f"<?php $OC_Version = array({installed},0,5,2);\n")
    pb = tmp_path / "pb.yml"
    pb.write_text(textwrap.dedent(f"""
        - hosts: localhost
          connection: local
          gather_facts: false
          vars: {{app: skhub, env: dev}}
          tasks:
            - import_tasks: {GUARD}
            - debug: msg="pending={{{{ skhub_nc_upgrade_pending }}}}"
    """))
    extra = yaml.safe_dump({"skhub": {"WEBROOT_LOCAL_PATH": str(tmp_path), "NEXTCLOUD_IMAGE": image}})
    (tmp_path / "x.yml").write_text(extra)
    r = subprocess.run(["ansible-playbook", str(pb), "-e", f"@{tmp_path / 'x.yml'}"],
                       capture_output=True, text=True, cwd=tmp_path,
                       env={"PATH": __import__("os").environ["PATH"], "HOME": str(tmp_path),
                            "ANSIBLE_LOCAL_TEMP": str(tmp_path / "t")})
    assert (r.returncode == 0) == ok, r.stdout + r.stderr
    if ok:
        assert f"pending={pending}" in r.stdout
    else:
        assert "one major at a time" in r.stdout
