"""skhome's deploy rendered through REAL Ansible (render/render_playbooks.py)
with a given vault: digest-pinned upstream image, one app replica, the
router built from the framework's host expressions, the middleware and SSO
knobs, the data path / data node guard, AUTH_SECRET written into the env
file (and nowhere else), the backup_time/backup_tz schedule guard, and loud
failures for a missing secret, an unpinned image or contradictory sign-in
settings. Sign-in: AUTH_MODE sso (default, Authentik forward auth on the
whole router) or local (HomeCenter's own accounts only, no OIDC upstream).
"""
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "skhome"
BASE = yaml.safe_load((rp.VARS / "skhome.example.yml").read_text())["skhome"]
SET = yaml.safe_load((rp.VARS / "skhome.set.yml").read_text())["skhome"]
IMAGE = ("ghcr.io/yatzin/homecenter:2.3.1@sha256:"
         "45bc4d6a0fed858ac8c89bd0631c69ab732093d5dd626e14a96678058e588bc4")


def render(tmp: Path, vault, env: str = "prod") -> tuple[dict, Path]:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    doc = {} if vault is None else {"skhome": vault}
    (vars_dir / "skhome.example.yml").write_text(yaml.safe_dump(doc))
    run_dir = work / f"skhome-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "skhome" / f"deploy_skhome-{env}.yml"
        res = rp.run_one(work, "optional", "skhome", env, pb, "base")
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
    return yaml.safe_load((out / f"var/data/config/skhome-{env}/skhome.yml").read_text())


def labels(svc):
    return dict(l.split("=", 1) for l in svc["deploy"]["labels"])


def env_file(out, env="prod"):
    return dict(l.split("=", 1) for l in (out / f"var/data/config/skhome-{env}/skhome.env").read_text()
                .splitlines() if l and not l.startswith("#"))


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skhome-base"), BASE)


@pytest.fixture(scope="module")
def set_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skhome-set"), BASE | SET)


# --- happy path: defaults --------------------------------------------------

def test_default_services_image_and_single_app_replica(base_out):
    svcs = compose(base_out)["services"]
    assert sorted(svcs) == ["app", "backup"]
    app, backup = svcs["app"], svcs["backup"]
    assert app["image"] == IMAGE and backup["image"] == IMAGE
    assert app["deploy"]["replicas"] == 1 and app["deploy"]["update_config"]["order"] == "stop-first"
    assert backup["deploy"]["replicas"] == 1
    for svc in (app, backup):
        assert svc["deploy"]["restart_policy"]["condition"] == "any"
        assert svc["deploy"]["placement"]["constraints"] == ["node.role == worker"]
    assert app["volumes"] == ["/var/data/skhome-prod/data:/data"]
    assert backup["volumes"] == [
        "/var/data/skhome-prod/data:/data",
        "/var/data/skhome-prod/backup:/backup",
        "/var/data/skhome-prod/bin/skhome-backup:/usr/local/bin/skhome-backup:ro",
    ]
    assert app["networks"] == ["skhome-prod", "cloud-public-prod"]
    assert backup["networks"] == ["skhome-prod"]
    assert app["healthcheck"]["test"] == ["CMD", "wget", "-qO-", "http://127.0.0.1:3000/api/health"]
    assert (app["healthcheck"]["interval"], app["healthcheck"]["timeout"], app["healthcheck"]["retries"],
            app["healthcheck"]["start_period"]) == ("30s", "10s", 3, "120s")
    assert backup["healthcheck"] == {"disable": True}
    assert backup["entrypoint"] == ["/bin/bash", "/usr/local/bin/skhome-backup"]
    # AUTH_SECRET/DATABASE_URL come from the SAME 0600 env_file as `app`,
    # never inline in this 0644 compose file.
    assert backup["env_file"] == ["/var/data/config/skhome-prod/skhome.env"]
    assert "AUTH_SECRET" not in backup["environment"]
    assert "DATABASE_URL" not in backup["environment"]
    assert backup["environment"]["BACKUP_FREQUENCY"] == "86400"
    assert backup["environment"]["BACKUP_START_DELAY"] == "60"
    assert "BACKUP_TIME" not in backup["environment"]


def test_default_auth_mode_is_sso_authentik_on_the_whole_router(base_out):
    lab = labels(compose(base_out)["services"]["app"])
    assert lab["traefik.http.routers.skhome.rule"] == "Host(`skhome.render.example.test`)"
    assert lab["traefik.http.routers.skhome.middlewares"] == "default-no-error-pages@file,authentik@file"
    assert lab["traefik.http.routers.skhome.tls.options"] == "intermediate@file"
    assert lab["traefik.http.services.skhome.loadbalancer.server.port"] == "3000"
    assert lab["traefik.http.services.skhome.loadbalancer.healthcheck.path"] == "/api/health"


@pytest.mark.parametrize("mode", ["SSO", "sso", ""])
def test_auth_mode_sso_spellings_render_like_the_default(tmp_path, base_out, mode):
    out = ok(tmp_path, BASE | {"AUTH_MODE": mode})
    rel = "var/data/config/skhome-prod/skhome.yml"
    assert (out / rel).read_text() == (base_out / rel).read_text()


def test_auth_mode_sso_takes_a_custom_forward_auth_middleware(tmp_path):
    out = ok(tmp_path, BASE | {"AUTH_MODE": "sso", "SSO_MIDDLEWARE": "authentik-other@file"})
    lab = labels(compose(out)["services"]["app"])
    assert lab["traefik.http.routers.skhome.middlewares"] == "default-no-error-pages@file,authentik-other@file"


def test_auth_mode_local_is_no_sso_middleware(tmp_path):
    out = ok(tmp_path, BASE | {"AUTH_MODE": "local"})
    lab = labels(compose(out)["services"]["app"])
    assert lab["traefik.http.routers.skhome.middlewares"] == "default-no-error-pages@file"


def test_admin_env_and_notify_defaults(base_out):
    env = env_file(base_out)
    assert env["ADMIN_NAME"] == "Admin"
    assert env["ADMIN_EMAIL"] == "admin@localhost"
    # No literal password default rendered (test_no_default_secrets.py):
    # HomeCenter's own seed falls back to "changeme" and forces a reset.
    assert "ADMIN_PASSWORD" not in env
    assert env["NOTIFY_INTERVAL_MINUTES"] == "360"
    assert env["DIGEST_HOUR"] == "8"
    assert env["DOCUMENT_REINDEX_HOURS"] == "6"
    assert env["EMBEDDING_IDLE_MINUTES"] == "10"
    assert env["MAX_UPLOAD_BYTES"] == "26214400"
    assert env["AUTH_URL"] == "https://skhome.render.example.test"
    assert "SMTP_HOST" not in env
    assert "SEARCH_INDEX_PATH" not in env


def test_backup_disabled_removes_the_service(tmp_path):
    out = ok(tmp_path, BASE | {"BACKUP_ENABLED": False})
    assert sorted(compose(out)["services"]) == ["app"]


# --- the "set" pass: every knob ------------------------------------------

def test_every_knob_set_renders(set_out):
    svcs = compose(set_out)["services"]
    app, backup = svcs["app"], svcs["backup"]
    assert app["image"] == SET["HOMECENTER_IMAGE"] and backup["image"] == SET["HOMECENTER_IMAGE"]
    assert app["volumes"] == [f"{SET['DATA_PATH']}:/data"]
    assert backup["volumes"][0] == f"{SET['DATA_PATH']}:/data"
    assert backup["volumes"][1] == f"{SET['BACKUP_PATH']}:/backup"
    assert app["deploy"]["placement"]["constraints"] == [
        "node.role == worker", "node.labels.skhome == true", "node.hostname == render-node-1",
    ]
    assert app["deploy"]["resources"]["limits"]["cpus"] == "2.00"
    assert app["deploy"]["resources"]["limits"]["memory"] == "1536M"
    assert app["deploy"]["resources"]["reservations"]["memory"] == "256M"
    assert (app["healthcheck"]["interval"], app["healthcheck"]["timeout"], app["healthcheck"]["retries"],
            app["healthcheck"]["start_period"]) == ("15s", "5s", 5, "60s")
    lab = labels(app)
    assert "traefik.http.routers.skhome.tls.options" not in lab
    assert lab["traefik.http.routers.skhome.rule"] == "Host(`home.example.test`)"
    env = env_file(set_out)
    assert env["ADMIN_NAME"] == "Example Admin"
    assert env["SMTP_HOST"] == "smtp.example.test"
    assert env["SMTP_PORT"] == "465"
    assert env["SMTP_USER"] == "example-user"
    assert env["SMTP_PASS"] == "example-smtp-password"
    assert env["SMTP_SECURE"] == "1"
    assert env["MAIL_DRY_RUN"] == "1"
    assert env["SEARCH_INDEX_PATH"] == "/data/custom-search-index.db"
    assert env["SOME_UPSTREAM_VAR"] == "example-value"
    backup_env = backup["environment"]
    assert backup_env["BACKUP_TIME"] == "03:30"
    assert backup_env["BACKUP_TZ"] == "America/New_York"
    assert "BACKUP_FREQUENCY" not in backup_env


def test_smtp_secure_false_renders_zero(tmp_path):
    out = ok(tmp_path, BASE | {"SMTP_HOST": "smtp.example.test", "SMTP_SECURE": False})
    env = env_file(out)
    assert env["SMTP_SECURE"] == "0"


# --- refusals ---------------------------------------------------------

def test_refuses_missing_auth_secret(tmp_path):
    log = fails(tmp_path, {"CLUSTERNAME": "render", "DOMAIN": "example.test"})
    assert "AUTH_SECRET is required" in log


def test_refuses_short_auth_secret(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_SECRET": "too-short"})
    assert "AUTH_SECRET is required" in log


def test_refuses_unpinned_image(tmp_path):
    log = fails(tmp_path, BASE | {"HOMECENTER_IMAGE": "ghcr.io/yatzin/homecenter:2.3.1"})
    assert "must be pinned by digest" in log


def test_refuses_unknown_auth_mode(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "oidc"})
    assert "AUTH_MODE must be" in log


def test_refuses_local_mode_with_sso_middleware_set(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "local", "SSO_MIDDLEWARE": "authentik@file"})
    assert "SSO_MIDDLEWARE is set" in log


def test_refuses_forward_auth_in_middlewares_with_local_mode(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "local", "TRAEFIK_MIDDLEWARES": "authentik@file"})
    assert "SSO under another name" in log


def test_refuses_local_data_path_without_data_node(tmp_path):
    log = fails(tmp_path, BASE | {"DATA_PATH": "/mnt/local/skhome/data"})
    assert "DATA_NODE is empty" in log


def test_refuses_bad_backup_time_format(tmp_path):
    log = fails(tmp_path, BASE | {"backup_time": "3:30am"})
    assert "backup_time must be 24-hour" in log


def test_refuses_unknown_backup_tz(tmp_path):
    log = fails(tmp_path, BASE | {"backup_time": "03:00", "backup_tz": "Mars/Olympus"})
    assert "is not a known IANA time zone" in log


# --- dev/staging parity -------------------------------------------------

@pytest.mark.parametrize("env", ["dev", "staging"])
def test_env_playbooks_render_with_their_own_subnet_and_host(tmp_path, env):
    out = ok(tmp_path, BASE, env=env)
    net = yaml.safe_load((out / f"var/data/config/skhome-{env}/skhome.yml").read_text())["networks"]
    assert f"skhome-{env}" in net
    app = compose(out, env)["services"]["app"]
    lab = labels(app)
    assert lab["traefik.http.routers.skhome-{0}.rule".format(env)] == f"Host(`skhome-{env}.render.example.test`)"
