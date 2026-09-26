"""Live-parity sweep item 5 flagged skhub's deploy.j2 as referencing
docker-compose.yml, a file supposedly never rendered (the real compose file
is skhub.yml). Checked against integration/v2.19.0: every deploy_skhub-*.yml
already renders {{ app }}.yml and then symlinks docker-compose.yml to it
(both under the same 'compose' tag, so a tag-scoped run can't create one
without the other). No framework change needed here; this test locks the
invariant in so it can't silently regress."""
import pathlib
import re

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"

ENVS = ["dev", "staging", "prod"]


def test_deploy_script_references_docker_compose_yml():
    deploy_j2 = (SKHUB / "src/skhub/deploy.j2").read_text()
    assert 'COMPOSE_FILE="${CONFIG_DIR}/docker-compose.yml"' in deploy_j2


def test_every_env_playbook_renders_and_symlinks_the_compose_file():
    for e in ENVS:
        pb = (SKHUB / f"deploy_skhub-{e}.yml").read_text()
        assert 'dest: "/var/data/config/{{ app }}-{{ env }}/{{ app }}.yml"' in pb, e
        m = re.search(
            r"Create docker-compose\.yml symlink\s*\n\s*file:\s*\n"
            r'\s*src: "(/var/data/config/\{\{ app \}\}-\{\{ env \}\}/\{\{ app \}\}\.yml)"\s*\n'
            r'\s*dest: "(/var/data/config/\{\{ app \}\}-\{\{ env \}\}/docker-compose\.yml)"',
            pb,
        )
        assert m, f"{e}: no docker-compose.yml symlink task pointing at the rendered {{{{ app }}}}.yml"
        assert "state: link" in pb.split("Create docker-compose.yml symlink")[1][:300]
