"""skreg's deploy role, pinned so a stale-base merge cannot silently undo it
again (#199's fix was reverted by an unrelated PR and the registry's deploy
script went back to sourcing an env file nothing rendered, removing a service
that does not exist, and deploying the stack twice in a row, which raced
swarm with "update out of sequence")."""
from pathlib import Path

import pytest

ROLE = Path(__file__).resolve().parents[1] / "ansible" / "optional" / "skreg"
ENVS = ("dev", "staging", "prod")


def test_deploy_script_never_removes_a_service_or_deploys_twice():
    text = (ROLE / "src" / "skreg" / "deploy.j2").read_text()
    assert "docker service rm" not in text
    assert text.count("docker stack deploy") == 1
    assert text.startswith("#!/bin/bash")
    assert "set -e" in text


def test_no_untemplated_deploy_script_left_behind():
    assert not (ROLE / "src" / "skreg" / "deploy").exists()


@pytest.mark.parametrize("env", ENVS)
def test_playbook_renders_both_env_files_it_sources(env):
    pb = (ROLE / f"deploy_skreg-{env}.yml").read_text()
    assert 'dest: "{{ app }}.env"' in pb
    assert 'dest: "{{ app }}.sh.env"' in pb
    assert "/docker-compose.yml" in pb  # the path deploy.j2 reads


def test_compose_loads_the_rendered_env_file():
    compose = (ROLE / "src" / "config" / "skreg" / "skreg.yml.j2").read_text()
    assert "env_file: /var/data/config/skreg-{{ env }}/skreg.env" in compose
    assert (ROLE / "src" / "config" / "skreg" / "skreg.env.j2").exists()
    assert (ROLE / "src" / "config" / "skreg" / "skreg.sh.env.j2").exists()


@pytest.mark.parametrize("env", ENVS)
def test_compose_file_is_root_only(env):
    # the release gate's secret-file-mode check pins skreg.yml at 600 0:0
    pb = (ROLE / f"deploy_skreg-{env}.yml").read_text()
    block = pb.split('dest: "/var/data/config/{{ app }}-{{ env }}/{{ app }}.yml"')[1].split("- name:")[0]
    assert 'mode: "0600"' in block
