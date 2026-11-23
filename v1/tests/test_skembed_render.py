"""skembed's deploy rendered through REAL Ansible: digest-pinned image, one
model source, the placement guard for local disk, the HF token and API key
only in the 0600 env file, and the default image equal in playbook and
compose."""
import re
import shutil
from pathlib import Path

import pytest
import yaml

import role_render_support as rs

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = Path(__file__).resolve().parents[1] / "ansible/optional/skembed"
BASE = rs.example("skembed")
SET = rs.example("skembed", "set")


def compose(out, env="prod"):
    return yaml.safe_load((out / f"var/data/config/skembed-{env}/skembed.yml").read_text())


def test_default_image_is_digest_pinned_and_equal_everywhere():
    pins = set()
    for env in ("dev", "staging", "prod"):
        pins |= set(re.findall(r'ske_default_image: "([^"]+)"', (ROLE / f"deploy_skembed-{env}.yml").read_text()))
    pins |= set(re.findall(r"default\('([^']+@sha256:[0-9a-f]{64})'", (ROLE / "src/config/skembed/skembed.yml.j2").read_text()))
    assert len(pins) == 1
    assert re.match(r"^ghcr\.io/huggingface/text-embeddings-inference:cpu-[0-9.]+@sha256:[0-9a-f]{64}$", pins.pop())


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_hf_repo_model(tmp_path, env):
    out = rs.ok(tmp_path, "skembed", BASE, env)
    svc = compose(out, env)["services"]["skembed"]
    envs = dict(e.split("=", 1) for e in svc["environment"])
    assert envs["MODEL_ID"] == BASE["MODEL_ID"]
    assert envs["PORT"] == "8080"
    assert svc["networks"] == [f"skembed-{env}"]
    assert "ports" not in svc
    assert svc["deploy"]["resources"]["limits"]["memory"] == "6G"
    assert "HF_TOKEN" not in (out / f"var/data/config/skembed-{env}/skembed.yml").read_text()


def test_local_model_dir_mounted_read_only_and_secrets_in_env_file(tmp_path):
    out = rs.ok(tmp_path, "skembed", {**BASE, **SET})
    svc = compose(out)["services"]["skembed"]
    envs = dict(e.split("=", 1) for e in svc["environment"])
    assert envs["MODEL_ID"] == "/model"
    assert f"{SET['MODEL_PATH']}:/model:ro" in svc["volumes"]
    assert envs["SERVED_MODEL_NAME"] == "example-embed"
    assert svc["ports"][0]["published"] == 18089
    assert svc["deploy"]["replicas"] == 2
    secrets = rs.env_file(out / "var/data/config/skembed-prod/skembed.env")
    assert secrets == {"HF_TOKEN": SET["HF_TOKEN"], "API_KEY": SET["API_KEY"]}
    text = (out / "var/data/config/skembed-prod/skembed.yml").read_text()
    assert SET["HF_TOKEN"] not in text and SET["API_KEY"] not in text


@pytest.mark.parametrize("vault,needle", [
    ({"MODEL_ID": "", "MODEL_PATH": ""}, "exactly one of MODEL_PATH"),
    ({"MODEL_ID": "a/b", "MODEL_PATH": "/var/data/m"}, "exactly one of MODEL_PATH"),
    ({"MODEL_ID": "not a repo"}, "exactly one of MODEL_PATH"),
    ({"MODEL_ID": "a/b", "IMAGE": "ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.4"}, "pinned by digest"),
    ({"MODEL_ID": "", "MODEL_PATH": "/srv/models/x"}, "no node.hostname"),
    ({"MODEL_ID": "a/b", "POOLING": "max"}, "POOLING one of"),
])
def test_bad_vaults_fail_closed(tmp_path, vault, needle):
    assert needle in rs.fails(tmp_path, "skembed", {**BASE, **vault})
