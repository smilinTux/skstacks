"""skpatch's deploy rendered through REAL Ansible (render/render_playbooks.py)
with a given vault: digest-pinned upstream image, one dashboard replica, the
router built from the framework's host expressions, the middleware and SSO
knobs (forward auth on the whole router, unlike skgym's split), the data
path/data node guard, and loud failures for a missing encryption key or an
unpinned image. ALLOW_ACTIONS is metadata only at this layer (the real
privilege enforcement is provision_skpatch_nodes.yml, covered in
test_skpatch_provision.py): here we only check it renders and reports,
never that it grants anything by itself."""
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "skpatch"
BASE = yaml.safe_load((rp.VARS / "skpatch.example.yml").read_text())["skpatch"]
SET = yaml.safe_load((rp.VARS / "skpatch.set.yml").read_text())["skpatch"]
IMAGE = ("ghcr.io/theduffman85/linux-update-dashboard:2026.10.9@sha256:"
         "1fb855a4d4b5eacc0ef3a9816064184492e9d068fb4da353c62871faeeeb1543")


def render(tmp: Path, vault, env: str = "prod") -> tuple[dict, Path]:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    doc = {} if vault is None else {"skpatch": vault}
    (vars_dir / "skpatch.example.yml").write_text(yaml.safe_dump(doc))
    run_dir = work / f"skpatch-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "skpatch" / f"deploy_skpatch-{env}.yml"
        res = rp.run_one(work, "optional", "skpatch", env, pb, "base")
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
    return yaml.safe_load((out / f"var/data/config/skpatch-{env}/skpatch.yml").read_text())


def labels(svc):
    return dict(l.split("=", 1) for l in svc["deploy"]["labels"])


def env_file(out, env="prod"):
    return dict(l.split("=", 1) for l in (out / f"var/data/config/skpatch-{env}/skpatch.env").read_text()
                .splitlines() if l and not l.startswith("#"))


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skpatch-base"), BASE)


@pytest.fixture(scope="module")
def set_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skpatch-set"), BASE | SET)


# --- happy path: defaults --------------------------------------------------

def test_default_service_image_and_single_replica(base_out):
    svcs = compose(base_out)["services"]
    assert sorted(svcs) == ["dashboard"]
    dashboard = svcs["dashboard"]
    assert dashboard["image"] == IMAGE
    assert dashboard["deploy"]["replicas"] == 1
    assert dashboard["deploy"]["update_config"]["order"] == "stop-first"
    assert dashboard["deploy"]["restart_policy"]["condition"] == "any"
    assert "max_attempts" not in dashboard["deploy"]["restart_policy"]
    assert dashboard["deploy"]["placement"]["constraints"] == ["node.role == worker"]
    assert dashboard["volumes"] == ["/var/data/skpatch-prod/data:/data"]
    assert dashboard["networks"] == ["skpatch-prod", "cloud-public-prod"]
    assert dashboard["healthcheck"]["test"] == ["CMD", "wget", "--spider", "-q", "http://127.0.0.1:3001/"]
    assert (dashboard["healthcheck"]["interval"], dashboard["healthcheck"]["timeout"],
            dashboard["healthcheck"]["retries"], dashboard["healthcheck"]["start_period"]) == ("30s", "10s", 3, "30s")


def test_default_auth_mode_is_sso_authentik_on_the_whole_router(base_out):
    lab = labels(compose(base_out)["services"]["dashboard"])
    # No API path is carved out: the whole router is behind forward auth.
    assert lab["traefik.http.routers.skpatch.rule"] == "Host(`skpatch.render.example.test`)"
    assert lab["traefik.http.routers.skpatch.middlewares"] == "default-no-error-pages@file,authentik@file"
    assert lab["traefik.http.routers.skpatch.service"] == "skpatch"
    assert lab["traefik.http.routers.skpatch.tls.options"] == "intermediate@file"
    assert not any(k.endswith("-ui.rule") for k in lab)


@pytest.mark.parametrize("mode", ["SSO", "sso", ""])
def test_auth_mode_sso_spellings_render_like_the_default(tmp_path, base_out, mode):
    out = ok(tmp_path, BASE | {"AUTH_MODE": mode})
    rel = "var/data/config/skpatch-prod/skpatch.yml"
    assert (out / rel).read_text() == (base_out / rel).read_text()


def test_auth_mode_sso_takes_a_custom_forward_auth_middleware(tmp_path):
    out = ok(tmp_path, BASE | {"AUTH_MODE": "sso", "SSO_MIDDLEWARE": "authentik-other@file"})
    lab = labels(compose(out)["services"]["dashboard"])
    assert lab["traefik.http.routers.skpatch.middlewares"] == "default-no-error-pages@file,authentik-other@file"


def test_auth_mode_local_is_one_router_without_sso(tmp_path):
    out = ok(tmp_path, BASE | {"AUTH_MODE": "local"})
    lab = labels(compose(out)["services"]["dashboard"])
    assert lab == {
        "com.skt.app": "skpatch", "com.skt.env": "prod",
        "traefik.enable": "true",
        "traefik.swarm.network": "cloud-public-prod",
        "traefik.http.routers.skpatch.rule": "Host(`skpatch.render.example.test`)",
        "traefik.http.routers.skpatch.entrypoints": "websecure",
        "traefik.http.routers.skpatch.tls": "true",
        "traefik.http.routers.skpatch.tls.certresolver": "main",
        "traefik.http.routers.skpatch.tls.options": "intermediate@file",
        "traefik.http.routers.skpatch.middlewares": "default-no-error-pages@file",
        "traefik.http.routers.skpatch.service": "skpatch",
        "traefik.http.services.skpatch.loadbalancer.server.port": "3001",
        "traefik.http.services.skpatch.loadbalancer.healthcheck.path": "/",
        "traefik.http.services.skpatch.loadbalancer.healthcheck.interval": "30s",
        "traefik.http.services.skpatch.loadbalancer.healthcheck.timeout": "10s",
    }


def test_default_env_binds_the_host_and_trusts_the_proxy(base_out):
    assert env_file(base_out) == {
        "LUDASH_ENCRYPTION_KEY": "example-ludash-key-not-a-secret-0123456789AB",
        "LUDASH_DB_PATH": "/data/dashboard.db",
        "LUDASH_BASE_URL": "https://skpatch.render.example.test",
        "LUDASH_PORT": "3001", "LUDASH_HOST": "0.0.0.0", "LUDASH_TRUST_PROXY": "true",
        "LUDASH_LOG_LEVEL": "info", "TZ": "UTC", "NODE_ENV": "production",
    }


def test_encryption_key_lands_only_in_the_root_only_env_file(base_out):
    secret = BASE["LUDASH_ENCRYPTION_KEY"]
    pb = (ROLE / "deploy_skpatch-prod.yml").read_text()
    for name in ("app env (root only)",):
        block = pb.split(name)[1].split("- name:")[0]
        assert "mode: '0600'" in block, name


@pytest.mark.parametrize("env,host,router,subnet", [
    ("dev", "skpatch-dev.render.example.test", "skpatch-dev", "172.16.156.0/24"),
    ("staging", "skpatch-staging.render.example.test", "skpatch-staging", "172.16.155.0/24"),
])
def test_dev_and_staging_hosts_routers_and_subnets(tmp_path, env, host, router, subnet):
    out = ok(tmp_path, BASE, env)
    lab = labels(compose(out, env)["services"]["dashboard"])
    assert lab[f"traefik.http.routers.{router}.rule"] == f"Host(`{host}`)"
    assert env_file(out, env)["LUDASH_BASE_URL"] == f"https://{host}"
    assert subnet in (ROLE / f"deploy_skpatch-{env}.yml").read_text()


# --- ALLOW_ACTIONS: metadata only at the deploy layer -----------------------

@pytest.mark.parametrize("value,word", [(False, "REPORT-ONLY"), (True, "ALLOWED")])
def test_allow_actions_only_changes_the_report_line(tmp_path, base_out, value, word):
    out = ok(tmp_path, BASE | {"ALLOW_ACTIONS": value})
    rel = "var/data/config/skpatch-prod/skpatch.yml"
    # The compose output (what actually ships) does not change at all: this
    # knob grants nothing at the swarm-deploy layer, by design.
    assert (out / rel).read_text() == (base_out / rel).read_text()
    pb = (ROLE / "deploy_skpatch-prod.yml").read_text()
    assert word in pb


# --- every knob set --------------------------------------------------------

def test_set_pass_pins_dashboard_to_the_data_node(set_out):
    svcs = compose(set_out)["services"]
    want = ["node.role == worker", "node.labels.skpatch == true", "node.hostname == render-node-1"]
    assert svcs["dashboard"]["deploy"]["placement"]["constraints"] == want
    assert svcs["dashboard"]["volumes"] == ["/var/lib/skpatch-render/data:/data"]
    assert svcs["dashboard"]["image"] == SET["IMAGE"]
    assert svcs["dashboard"]["deploy"]["resources"]["limits"] == {"cpus": "1.00", "memory": "768M"}


def test_set_pass_env_carries_every_setting(set_out):
    env = env_file(set_out)
    assert env["LUDASH_LOG_LEVEL"] == "debug" and env["TZ"] == "Europe/Zurich"
    assert env["SOME_UPSTREAM_VAR"] == "example-value"


def test_set_pass_splits_middlewares_and_omits_tls_options(set_out):
    lab = labels(compose(set_out)["services"]["dashboard"])
    assert lab["traefik.http.routers.skpatch.middlewares"] == "crowdsec-bouncer@file,default-no-error-pages@file,authentik@file"
    assert not any(k.endswith(".tls.options") for k in lab)


# --- failures ---------------------------------------------------------------

@pytest.mark.parametrize("vault", [
    None,                                                                   # no skpatch block at all
    {k: v for k, v in BASE.items() if k != "LUDASH_ENCRYPTION_KEY"},        # no secret
    BASE | {"LUDASH_ENCRYPTION_KEY": ""},                                   # empty secret
    BASE | {"LUDASH_ENCRYPTION_KEY": "too-short-key"},                      # under 32 characters
], ids=["no-block", "missing", "empty", "short"])
def test_missing_or_weak_encryption_key_fails_loudly(tmp_path, vault):
    log = fails(tmp_path, vault)
    assert "skpatch.LUDASH_ENCRYPTION_KEY is required" in log
    assert "too-short-key" not in log


def test_unpinned_image_fails(tmp_path):
    log = fails(tmp_path, BASE | {"IMAGE": "ghcr.io/theduffman85/linux-update-dashboard:latest"})
    assert "must be pinned by digest" in log


@pytest.mark.parametrize("mode", ["oidc", "authentik", "off", "none"])
def test_unknown_auth_mode_fails(tmp_path, mode):
    log = fails(tmp_path, BASE | {"AUTH_MODE": mode})
    assert "skpatch.AUTH_MODE must be 'sso' or 'local'" in log


def test_auth_mode_local_refuses_an_sso_middleware(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "local", "SSO_MIDDLEWARE": "authentik@file"})
    assert "AUTH_MODE is 'local'" in log and "SSO_MIDDLEWARE" in log


@pytest.mark.parametrize("mode", ["sso", "local"])
def test_forward_auth_in_traefik_middlewares_is_refused(tmp_path, mode):
    log = fails(tmp_path, BASE | {"AUTH_MODE": mode, "TRAEFIK_MIDDLEWARES": "default@file,authentik@file"})
    assert "TRAEFIK_MIDDLEWARES" in log and "forward auth" in log


def test_local_data_path_without_data_node_fails(tmp_path):
    log = fails(tmp_path, BASE | {"DATA_PATH": "/srv/skpatch/data"})
    assert "DATA_NODE is empty" in log


# --- static ------------------------------------------------------------------

def test_playbook_default_image_equals_the_compose_literal():
    template = (ROLE / "src/config/skpatch/skpatch.yml.j2").read_text()
    for env in ("dev", "staging", "prod"):
        pb = (ROLE / f"deploy_skpatch-{env}.yml").read_text()
        default = re.search(r'skp_default_image: "([^"]+)"', pb).group(1)
        assert default == IMAGE, env
        assert template.count(f"default('{IMAGE}', true)") == 1


def test_env_playbooks_differ_only_in_env_and_subnets():
    def norm(text):
        text = re.sub(r"(?<![A-Za-z])(dev|staging|prod)(?![A-Za-z])", "ENV", text)
        return re.sub(r"\d+\.\d+\.\d+\.\d+/\d+", "CIDR", text)
    prod = norm((ROLE / "deploy_skpatch-prod.yml").read_text())
    for env in ("dev", "staging"):
        assert norm((ROLE / f"deploy_skpatch-{env}.yml").read_text()) == prod, env
