"""Runner stack pins: dind and its cleanup sidecar are digest-pinned on the
same docker major, the runner version is an overridable var, and the
`docker-cli` label tracks the dind major (client and daemon stay aligned)."""
import pathlib
import re

import jinja2
import yaml

CFG = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skgit/src/config/skgit"


def render(name, **skgit):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string((CFG / name).read_text()).render(env="dev", skgit=skgit)


def services():
    return yaml.safe_load(render("skgit-runners.yml.j2"))["services"]


def test_dind_and_cleanup_are_digest_pinned_on_one_major():
    svc = services()
    pat = re.compile(r"^docker:(\d+)\.\d+\.\d+-(dind|cli)@sha256:[0-9a-f]{64}$")
    dind = pat.match(svc["dind"]["image"])
    cleanup = pat.match(svc["dind-cleanup"]["image"])
    assert dind and dind.group(2) == "dind"
    assert cleanup and cleanup.group(2) == "cli"
    assert dind.group(1) == cleanup.group(1)


def test_runner_version_default_and_override():
    assert re.fullmatch(r"code\.forgejo\.org/forgejo/runner:\d+\.\d+\.\d+", services()["forgejo-runner"]["image"])
    out = yaml.safe_load(render("skgit-runners.yml.j2", RUNNER_VERSION="12.13.2"))["services"]
    assert out["forgejo-runner"]["image"] == "code.forgejo.org/forgejo/runner:12.13.2"


def test_docker_cli_label_matches_dind_major():
    major = re.match(r"docker:(\d+)\.", services()["dind"]["image"]).group(1)
    assert f"docker://docker:{major}-cli" in render("runner-config.yml.j2")
    assert f"docker://docker:{major}-cli" in services()["forgejo-runner"]["command"]


def test_runner_config_has_no_keys_removed_in_runner_13():
    cfg = yaml.safe_load(render("runner-config.yml.j2"))
    assert "network_mode" not in cfg["container"]
