"""skwrit's deploy rendered through REAL Ansible (render/render_playbooks.py)
with a given vault: the digest-pinned worker, SKWRIT_* environment, external
secrets and volumes referenced by name, the stack and service naming knobs,
the optional Nextcloud MCP service, and loud failures for an unpinned or
missing image and malformed lists."""
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "skwrit"
BASE = yaml.safe_load((rp.VARS / "skwrit.example.yml").read_text())["skwrit"]
SET = yaml.safe_load((rp.VARS / "skwrit.set.yml").read_text())["skwrit"]
MCP_IMAGE = ("ghcr.io/cbcoutinho/nextcloud-mcp-server:0.199.4@sha256:"
             "4db27920ca54f13965f8a509bcaf8491c2d06a476cc1b77062868ae901c2f0f1")


def render(tmp: Path, vault, env: str = "prod") -> tuple[dict, Path]:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    doc = {} if vault is None else {"skwrit": vault}
    (vars_dir / "skwrit.example.yml").write_text(yaml.safe_dump(doc))
    run_dir = work / f"skwrit-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "skwrit" / f"deploy_skwrit-{env}.yml"
        res = rp.run_one(work, "optional", "skwrit", env, pb, "base")
    finally:
        rp.VARS = saved
    res["log"] = (run_dir / "ansible.log").read_text() if (run_dir / "ansible.log").exists() else ""
    return res, run_dir / "out"


def ok(tmp, vault, env="prod"):
    res, out = render(tmp, vault, env)
    assert res["status"] == "PASS", (res["findings"], res["log"][-3000:])
    return out


def fails(tmp, vault, env="prod"):
    res, _ = render(tmp, vault, env)
    assert res["status"] == "FAIL", "the deploy rendered although it must refuse this vault"
    return res["log"]


def compose(out, env="prod"):
    return yaml.safe_load((out / f"var/data/config/skwrit-{env}/skwrit.yml").read_text())


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("base"), BASE)


@pytest.fixture(scope="module")
def set_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("set"), BASE | SET)


def test_default_worker(base_out):
    c = compose(base_out)
    assert list(c["services"]) == ["worker"]
    w = c["services"]["worker"]
    assert w["image"] == BASE["SKWRIT_IMAGE"]
    assert w["environment"] == {"NEXTCLOUD_URL": BASE["NEXTCLOUD_URL"]}
    assert [(s["source"], s["target"]) for s in w["secrets"]] == [
        ("skwrit_nextcloud_v1", "nextcloud_password"),
        ("skwrit_worker_v1", "worker_token"),
        ("skwrit_vector_v1", "vector_api_key")]
    assert w["networks"] == ["internal", "workflow-apps"]
    assert w["volumes"] == ["state:/state"]
    d = w["deploy"]
    assert d["replicas"] == 1 and d["placement"]["constraints"] == ["node.role == worker"]
    assert d["resources"]["limits"]["memory"] == "2G"
    assert d["update_config"] == {"parallelism": 1, "order": "stop-first", "failure_action": "rollback"}
    assert "extra_hosts" not in w
    assert c["networks"]["internal"] == {"name": "skwrit-prod", "driver": "overlay",
                                         "ipam": {"config": [{"subnet": "172.16.249.0/24"}]}}
    assert c["networks"]["workflow-apps"] == {"external": True, "name": "skflow-prod"}
    assert c["volumes"] == {"state": {"external": True, "name": "skwrit-prod_state"}}
    assert set(c["secrets"]) == {"skwrit_nextcloud_v1", "skwrit_worker_v1", "skwrit_vector_v1"}


def test_compose_only_names_secrets(base_out):
    text = (base_out / "var/data/config/skwrit-prod/skwrit.yml").read_text()
    assert text.count("external: true") == 5 and "password" not in text.replace("nextcloud_password", "")


def test_set_pass_everything_set(set_out):
    c = compose(set_out)
    assert list(c["services"]) == ["engine-worker", "nextcloud-mcp"]
    w = c["services"]["engine-worker"]
    assert w["environment"]["SKWRIT_MODEL"] == "example-composer"
    assert w["environment"]["SKWRIT_REVIEW_MODEL"] == "example-reviewer"
    assert w["extra_hosts"] == ["models.example.test:192.0.2.10"]
    assert w["deploy"]["placement"]["constraints"] == ["node.hostname == render-node-1"]
    assert w["deploy"]["resources"]["limits"]["memory"] == "1G"
    assert c["networks"]["internal"]["ipam"]["config"] == [{"subnet": "192.0.2.0/24"}]
    assert c["networks"]["workflow-apps"]["name"] == "render-flow"
    assert c["volumes"]["state"]["name"] == "render_state"
    assert c["volumes"]["mcp_data"]["name"] == "render_mcp_data"
    mcp = c["services"]["nextcloud-mcp"]
    assert mcp["image"] == MCP_IMAGE
    assert mcp["environment"]["NEXTCLOUD_USERNAME"] == "svc-render"
    assert [s["target"] for s in mcp["secrets"]] == ["nextcloud_password"]


def test_deploy_script_checks_prerequisites_by_name(set_out):
    text = (set_out / "var/data/skwrit-prod/deploy").read_text()
    assert "docker secret inspect" in text and "render_worker_v1" in text
    assert "docker network inspect render-flow" in text
    assert "STACK_NAME=skwrit-render" in text and "_engine-worker" in text


def test_keep_an_existing_service_dns_name(tmp_path):
    out = ok(tmp_path, BASE | {"STACK_NAME": "legacy-prod", "WORKER_SERVICE": "legacy-worker"})
    c = compose(out)
    assert list(c["services"]) == ["legacy-worker"]
    assert c["networks"]["internal"]["name"] == "legacy-prod"


@pytest.mark.parametrize("env,subnet", [("dev", "172.16.253.128/25"), ("staging", "172.16.253.0/25")])
def test_dev_and_staging_names_and_subnets(tmp_path, env, subnet):
    c = compose(ok(tmp_path, BASE, env), env)
    assert c["networks"]["internal"]["ipam"]["config"] == [{"subnet": subnet}]
    assert c["networks"]["workflow-apps"]["name"] == f"skflow-{env}"


def test_missing_vault_block_fails(tmp_path):
    fails(tmp_path, None)


@pytest.mark.parametrize("value", ["", "registry.example.test/w:1", "registry.example.test/w@sha256x"])
def test_unpinned_or_missing_image_fails(tmp_path, value):
    fails(tmp_path, BASE | {"SKWRIT_IMAGE": value})


def test_unpinned_mcp_image_fails_only_when_enabled(tmp_path):
    ok(tmp_path, BASE | {"NEXTCLOUD_MCP_IMAGE": "x:1"})
    fails(tmp_path, BASE | {"NEXTCLOUD_MCP_ENABLED": True, "NEXTCLOUD_MCP_IMAGE": "x:1"})


@pytest.mark.parametrize("patch", [
    {"NEXTCLOUD_URL": ""}, {"NEXTCLOUD_URL": "files.example.test"},
    {"STACK_NAME": "Bad Name"}, {"WORKER_SERVICE": "a_b"},
    {"EXTRA_HOSTS": "models:192.0.2.1"}, {"EXTRA_HOSTS": ["no-address"]},
    {"PLACEMENT_CONSTRAINTS": "node.role == worker"},
])
def test_malformed_settings_fail(tmp_path, patch):
    fails(tmp_path, BASE | patch)


def test_default_mcp_image_literals_are_equal():
    template = (ROLE / "src/config/skwrit/skwrit.yml.j2").read_text()
    assert template.count(f"default('{MCP_IMAGE}', true)") == 1
    for env in ("dev", "staging", "prod"):
        assert (ROLE / f"deploy_skwrit-{env}.yml").read_text().count(f"default('{MCP_IMAGE}', true)") == 1, env


def test_env_playbooks_differ_only_in_env_and_subnets():
    def norm(text):
        text = re.sub(r"(?<![A-Za-z])(dev|staging|prod)(?![A-Za-z])", "ENV", text)
        return re.sub(r"\d+\.\d+\.\d+\.\d+/\d+", "CIDR", text)
    prod = norm((ROLE / "deploy_skwrit-prod.yml").read_text())
    for env in ("dev", "staging"):
        assert norm((ROLE / f"deploy_skwrit-{env}.yml").read_text()) == prod, env
