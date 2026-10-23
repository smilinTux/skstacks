"""sksec-web's deploy rendered through REAL Ansible (render/render_playbooks.py)
with a given vault: digest-pinned upstream image, one app replica, the
router built from the framework's host expressions, the middleware and SSO
knobs, the data path / data node guard, LAPI_PASSWORD written into the env
file (and nowhere else), the read-only default, and loud failures for a
missing secret, an unpinned image, an unknown AUTH_MODE, AUTH_MODE local
without the explicit public-risk flag, and contradictory sign-in settings.
Sign-in: AUTH_MODE sso (default, Authentik forward auth on the whole
router) or local (the dashboard's own accounts only, refused unless the
operator confirms the risk).
"""
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "sksec-web"
BASE = yaml.safe_load((rp.VARS / "sksec-web.example.yml").read_text())["sksec_web"]
SET = yaml.safe_load((rp.VARS / "sksec-web.set.yml").read_text())["sksec_web"]
IMAGE = ("ghcr.io/theduffman85/crowdsec-web-ui:2026.10.10@sha256:"
         "f57568d27b2c7798155a638b9cb76302777a9dcf43d8026f344c17767589d694")


def render(tmp: Path, vault, env: str = "prod") -> tuple[dict, Path]:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    doc = {} if vault is None else {"sksec_web": vault}
    (vars_dir / "sksec-web.example.yml").write_text(yaml.safe_dump(doc))
    run_dir = work / f"sksec-web-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "sksec-web" / f"deploy_sksec-web-{env}.yml"
        res = rp.run_one(work, "optional", "sksec-web", env, pb, "base")
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
    return yaml.safe_load((out / f"var/data/config/sksec-web-{env}/sksec-web.yml").read_text())


def labels(svc):
    return dict(l.split("=", 1) for l in svc["deploy"]["labels"])


def env_file(out, env="prod"):
    return dict(l.split("=", 1) for l in (out / f"var/data/config/sksec-web-{env}/sksec-web.env").read_text()
                .splitlines() if l and not l.startswith("#"))


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("sksec-web-base"), BASE)


@pytest.fixture(scope="module")
def set_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("sksec-web-set"), BASE | SET)


# --- happy path: defaults --------------------------------------------------

def test_default_service_image_and_single_app_replica(base_out):
    svcs = compose(base_out)["services"]
    assert sorted(svcs) == ["app"]
    app = svcs["app"]
    assert app["image"] == IMAGE
    assert app["deploy"]["replicas"] == 1
    assert app["deploy"]["update_config"]["order"] == "stop-first"
    assert app["deploy"]["restart_policy"]["condition"] == "any"
    assert app["deploy"]["placement"]["constraints"] == ["node.role == worker"]
    assert app["volumes"] == ["/var/data/sksec-web-prod/data:/app/data"]
    assert app["networks"] == ["sksec-prod", "cloud-public-prod"]
    assert app["healthcheck"]["test"] == ["CMD", "wget", "-qO-", "http://127.0.0.1:3000/api/health"]
    assert (app["healthcheck"]["interval"], app["healthcheck"]["timeout"], app["healthcheck"]["retries"],
            app["healthcheck"]["start_period"]) == ("30s", "5s", 3, "15s")


def test_default_auth_mode_is_sso_authentik_on_the_whole_router(base_out):
    lab = labels(compose(base_out)["services"]["app"])
    assert lab["traefik.http.routers.sksec-web.rule"] == "Host(`sksec-web.render.example.test`)"
    assert lab["traefik.http.routers.sksec-web.middlewares"] == "default-no-error-pages@file,authentik@file"
    assert lab["traefik.http.routers.sksec-web.tls.options"] == "intermediate@file"
    assert lab["traefik.http.services.sksec-web.loadbalancer.server.port"] == "3000"
    assert lab["traefik.http.services.sksec-web.loadbalancer.healthcheck.path"] == "/api/health"


@pytest.mark.parametrize("mode", ["SSO", "sso", ""])
def test_auth_mode_sso_spellings_render_like_the_default(tmp_path, base_out, mode):
    out = ok(tmp_path, BASE | {"AUTH_MODE": mode})
    rel = "var/data/config/sksec-web-prod/sksec-web.yml"
    assert (out / rel).read_text() == (base_out / rel).read_text()


def test_auth_mode_sso_takes_a_custom_forward_auth_middleware(tmp_path):
    out = ok(tmp_path, BASE | {"AUTH_MODE": "sso", "SSO_MIDDLEWARE": "authentik-other@file"})
    lab = labels(compose(out)["services"]["app"])
    assert lab["traefik.http.routers.sksec-web.middlewares"] == "default-no-error-pages@file,authentik-other@file"


def test_auth_mode_local_with_risk_flag_is_no_sso_middleware(tmp_path):
    out = ok(tmp_path, BASE | {"AUTH_MODE": "local", "ALLOW_LOCAL_PUBLIC_RISK": True})
    lab = labels(compose(out)["services"]["app"])
    assert lab["traefik.http.routers.sksec-web.middlewares"] == "default-no-error-pages@file"


def test_default_env_is_read_only_and_LAPI_url(base_out):
    env = env_file(base_out)
    assert env["PERMISSION_READ_ONLY"] == "true"
    assert env["CROWDSEC_URL"] == "http://sksec-prod_crowdsec:8080"
    assert env["CROWDSEC_USER"] == "sksec-web"
    assert env["CROWDSEC_PASSWORD"] == BASE["LAPI_PASSWORD"]
    assert env["CROWDSEC_LOOKBACK_PERIOD"] == "168h"
    assert "AUTH_SECRET" not in env


# --- the "set" pass: every knob ------------------------------------------

def test_every_knob_set_renders(set_out):
    app = compose(set_out)["services"]["app"]
    assert app["image"] == SET["CROWDSEC_WEB_UI_IMAGE"]
    assert app["volumes"] == [f"{SET['DATA_PATH']}:/app/data"]
    assert app["deploy"]["placement"]["constraints"] == [
        "node.role == worker", "node.labels.sksec_web == true", "node.hostname == render-node-1",
    ]
    assert app["deploy"]["resources"]["limits"]["cpus"] == "1.00"
    assert app["deploy"]["resources"]["limits"]["memory"] == "768M"
    assert app["deploy"]["resources"]["reservations"]["memory"] == "128M"
    assert (app["healthcheck"]["interval"], app["healthcheck"]["timeout"], app["healthcheck"]["retries"],
            app["healthcheck"]["start_period"]) == ("15s", "5s", 5, "30s")
    lab = labels(app)
    assert "traefik.http.routers.sksec-web.tls.options" not in lab
    assert lab["traefik.http.routers.sksec-web.rule"] == "Host(`sksec-web.example.test`)"
    env = env_file(set_out)
    assert env["PERMISSION_READ_ONLY"] == "false"
    assert env["CROWDSEC_USER"] == "sksec-web-render"
    assert env["CROWDSEC_URL"] == "http://sksec-render-crowdsec.example.test:8080"
    assert env["CROWDSEC_LOOKBACK_PERIOD"] == "72h"
    assert env["AUTH_SECRET"] == SET["SESSION_SECRET"]
    assert env["SOME_UPSTREAM_VAR"] == "example-value"


# --- refusals ---------------------------------------------------------

def test_refuses_missing_lapi_password(tmp_path):
    log = fails(tmp_path, {"CLUSTERNAME": "render", "DOMAIN": "example.test"})
    assert "LAPI_PASSWORD is required" in log


def test_refuses_short_lapi_password(tmp_path):
    log = fails(tmp_path, BASE | {"LAPI_PASSWORD": "too-short"})
    assert "LAPI_PASSWORD is required" in log


def test_refuses_unpinned_image(tmp_path):
    log = fails(tmp_path, BASE | {"CROWDSEC_WEB_UI_IMAGE": "ghcr.io/theduffman85/crowdsec-web-ui:2026.10.10"})
    assert "must be pinned by digest" in log


def test_refuses_unknown_auth_mode(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "oidc"})
    assert "AUTH_MODE must be" in log


def test_refuses_local_mode_without_explicit_risk_flag(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "local"})
    assert "removes Authentik forward auth" in log


def test_refuses_local_mode_with_sso_middleware_set(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "local", "ALLOW_LOCAL_PUBLIC_RISK": True, "SSO_MIDDLEWARE": "authentik@file"})
    assert "SSO_MIDDLEWARE is set" in log


def test_refuses_forward_auth_in_middlewares_with_local_mode(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "local", "ALLOW_LOCAL_PUBLIC_RISK": True, "TRAEFIK_MIDDLEWARES": "authentik@file"})
    assert "SSO under another name" in log


def test_refuses_local_data_path_without_data_node(tmp_path):
    log = fails(tmp_path, BASE | {"DATA_PATH": "/mnt/local/sksec-web/data"})
    assert "DATA_NODE is empty" in log


# --- dev/staging parity -------------------------------------------------

@pytest.mark.parametrize("env", ["dev", "staging"])
def test_env_playbooks_render_with_their_own_subnet_and_host(tmp_path, env):
    out = ok(tmp_path, BASE, env=env)
    net = yaml.safe_load((out / f"var/data/config/sksec-web-{env}/sksec-web.yml").read_text())["networks"]
    assert f"sksec-{env}" in net
    app = compose(out, env)["services"]["app"]
    lab = labels(app)
    assert lab["traefik.http.routers.sksec-web-{0}.rule".format(env)] == f"Host(`sksec-web-{env}.render.example.test`)"
