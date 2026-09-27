"""skha's per-check health-check script (skha_check.sh.j2) can only SKIP a
check when its container_pattern is absent, never fail it. NAM's old
per-node health scripts ASSERTED certain containers were present (VRRP
should fail over if the local Traefik/mail container is missing) for 2 of
its 4 checks. `require: true` on a health_checks entry closes that gap:
the check fails (exit 1) instead of silently passing when the named
container isn't running. Default (require absent/false) is unchanged:
skip-if-absent, exit 0."""
import pathlib
import re

import jinja2

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skha/src/keepalived/scripts/skha_check.sh.j2"


def render(pattern="traefik-worker", require=None, ports=(80, 443)):
    item = {"name": "traefik", "ports": list(ports)}
    if pattern is not None:
        item["container_pattern"] = pattern
    if require is not None:
        item["require"] = require
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string(SCRIPT.read_text()).render(item=item)


def _absent_branch(text):
    m = re.search(r"if ! docker ps.*?then\n(.*?)\n    fi", text, re.S)
    assert m, "container-presence branch not found in rendered script"
    return m.group(1)


def test_default_skips_the_check_when_container_absent():
    branch = _absent_branch(render())
    assert "exit 0" in branch
    assert "exit 1" not in branch


def test_require_false_is_identical_to_unset():
    assert render(require=False) == render()


def test_require_true_fails_the_check_when_container_absent():
    branch = _absent_branch(render(require=True))
    assert "exit 1" in branch
    assert "exit 0" not in branch


def test_no_container_pattern_has_no_docker_block_regardless_of_require():
    text = render(pattern=None, require=True)
    assert "docker ps" not in text
    assert text == render(pattern=None)


def test_require_true_still_runs_port_checks_after_the_docker_block():
    text = render(require=True)
    assert "ss -tuln" in text
    assert "port 80 not listening" in text
