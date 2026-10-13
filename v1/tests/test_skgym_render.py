"""skgym's deploy rendered through REAL Ansible (render/render_playbooks.py)
with a given vault: digest-pinned upstream images, one api replica, the
router built from the framework's host expressions, the middleware and SSO
knobs, the data path / data node guard, the session secret written to the
data dir (and nowhere else), and loud failures for a missing secret or an
unpinned image."""
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "skgym"
BASE = yaml.safe_load((rp.VARS / "skgym.example.yml").read_text())["skgym"]
SET = yaml.safe_load((rp.VARS / "skgym.set.yml").read_text())["skgym"]
API_IMAGE = ("ghcr.io/duartesantos8/opengym-api:1.4.1@sha256:"
             "4bc07fe7d1f05a57c0fa25da3563d54dfe27223506637d67fc64088a2a2f140b")
WEB_IMAGE = ("ghcr.io/duartesantos8/opengym-web:1.4.1@sha256:"
             "e32cae9d2f4aad0c5e1b7888c9bfc712b2306ddfe3c65bc4cfefd4b0002e86d3")


def render(tmp: Path, vault, env: str = "prod") -> tuple[dict, Path]:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    doc = {} if vault is None else {"skgym": vault}
    (vars_dir / "skgym.example.yml").write_text(yaml.safe_dump(doc))
    run_dir = work / f"skgym-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "skgym" / f"deploy_skgym-{env}.yml"
        res = rp.run_one(work, "optional", "skgym", env, pb, "base")
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
    return yaml.safe_load((out / f"var/data/config/skgym-{env}/skgym.yml").read_text())


def labels(svc):
    return dict(l.split("=", 1) for l in svc["deploy"]["labels"])


def env_file(out, env="prod"):
    return dict(l.split("=", 1) for l in (out / f"var/data/config/skgym-{env}/skgym.env").read_text()
                .splitlines() if l and not l.startswith("#"))


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skgym-base"), BASE)


@pytest.fixture(scope="module")
def set_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skgym-set"), BASE | SET)


# --- happy path: defaults --------------------------------------------------

def test_default_services_images_and_single_api_replica(base_out):
    svcs = compose(base_out)["services"]
    assert sorted(svcs) == ["api", "backup", "web"]
    api, web, backup = svcs["api"], svcs["web"], svcs["backup"]
    assert api["image"] == API_IMAGE and backup["image"] == API_IMAGE and web["image"] == WEB_IMAGE
    assert api["deploy"]["replicas"] == 1 and api["deploy"]["update_config"]["order"] == "stop-first"
    assert web["deploy"]["replicas"] == 1 and backup["deploy"]["replicas"] == 1
    for svc in (api, web, backup):
        assert svc["deploy"]["restart_policy"]["condition"] == "any"
        assert "max_attempts" not in svc["deploy"]["restart_policy"]
        assert svc["deploy"]["placement"]["constraints"] == ["node.role == worker"]
    assert api["volumes"] == ["/var/data/skgym-prod/data:/data"]
    assert backup["volumes"] == ["/var/data/skgym-prod/data:/data:ro", "/var/data/skgym-prod/backup:/backup",
                                 "/var/data/skgym-prod/bin/skgym-backup:/usr/local/bin/skgym-backup:ro"]
    assert api["networks"] == ["skgym-prod"]
    assert web["networks"] == ["skgym-prod", "cloud-public-prod"]
    assert api["healthcheck"]["test"] == ["CMD", "wget", "--spider", "-q", "http://127.0.0.1:3000/api/health"]
    assert web["healthcheck"]["test"] == ["CMD", "wget", "--spider", "-q", "http://127.0.0.1:8080/"]
    assert (api["healthcheck"]["interval"], api["healthcheck"]["timeout"], api["healthcheck"]["retries"],
            api["healthcheck"]["start_period"]) == ("30s", "10s", 3, "30s")
    assert backup["healthcheck"] == {"disable": True}
    assert backup["entrypoint"] == ["/bin/sh", "/usr/local/bin/skgym-backup"] and backup["command"] == ["loop"]
    assert web["environment"]["NGINX_PORT"] == "8080" and web["environment"]["BACKEND"] == "api"


def test_default_router_is_one_host_rule_without_crowdsec_or_sso(base_out):
    lab = labels(compose(base_out)["services"]["web"])
    assert lab == {
        "com.skt.app": "skgym", "com.skt.env": "prod",
        "traefik.enable": "true",
        "traefik.swarm.network": "cloud-public-prod",
        "traefik.http.routers.skgym.rule": "Host(`skgym.render.example.test`)",
        "traefik.http.routers.skgym.entrypoints": "websecure",
        "traefik.http.routers.skgym.tls": "true",
        "traefik.http.routers.skgym.tls.certresolver": "main",
        "traefik.http.routers.skgym.tls.options": "intermediate@file",
        "traefik.http.routers.skgym.middlewares": "default-no-crowdsec@file",
        "traefik.http.routers.skgym.service": "skgym",
        "traefik.http.services.skgym.loadbalancer.server.port": "8080",
        "traefik.http.services.skgym.loadbalancer.healthcheck.path": "/api/health",
        "traefik.http.services.skgym.loadbalancer.healthcheck.interval": "30s",
        "traefik.http.services.skgym.loadbalancer.healthcheck.timeout": "10s",
    }
    # the api is never on the Traefik network and carries no router
    assert not any(k.startswith("traefik") for k in labels(compose(base_out)["services"]["api"]))


def test_default_env_binds_passkeys_to_the_host_and_keeps_the_coach_off(base_out):
    assert env_file(base_out) == {
        "RP_ID": "skgym.render.example.test", "ORIGIN": "https://skgym.render.example.test",
        "RP_NAME": "openGym", "PORT": "3000", "DATA_DIR": "/data", "TRUST_PROXY": "1",
        "FIRST_USER_ADMIN": "1", "ADMIN_UIDS": "", "INVITE_ONLY": "0", "ALLOW_GUEST": "1",
        "PASSWORD_LOGIN": "0", "DEFAULT_LANG": "", "SESSION_DAYS": "90", "AUDIT_LOG": "1",
        "AUDIT_IP": "off", "AUDIT_MAX": "5000", "AUDIT_DAYS": "90", "MEDIA_UPLOADS": "1",
        "MEDIA_QUOTA_MB": "200", "COACH_DISABLED": "1", "TZ": "UTC",
    }


def test_session_secret_lands_only_in_the_root_only_secret_file(base_out):
    secret = BASE["SESSION_SECRET"]
    assert (base_out / "_copy/var/data/skgym-prod/data/secret").read_text() == secret + "\n"
    for f in (base_out / "var").rglob("*"):
        if f.is_file():
            assert secret not in f.read_text(), f
    pb = (ROLE / "deploy_skgym-prod.yml").read_text()
    block = pb.split("Write the session secret")[1].split("- name:")[0]
    assert "mode: '0600'" in block and "no_log: true" in block
    for name in ("api env (root only)",):
        block = pb.split(name)[1].split("- name:")[0]
        assert "mode: '0600'" in block, name


def test_backup_script_is_copied_verbatim(base_out):
    copied = base_out / "_copy/var/data/skgym-prod/bin/skgym-backup"
    assert copied.read_text() == (ROLE / "src/skgym/skgym-backup").read_text()


@pytest.mark.parametrize("env,host,router,subnet", [
    ("dev", "skgym-dev.render.example.test", "skgym-dev", "172.16.167.0/24"),
    ("staging", "skgym-staging.render.example.test", "skgym-staging", "172.16.166.0/24"),
])
def test_dev_and_staging_hosts_routers_and_subnets(tmp_path, env, host, router, subnet):
    out = ok(tmp_path, BASE, env)
    web = compose(out, env)["services"]["web"]
    lab = labels(web)
    assert lab[f"traefik.http.routers.{router}.rule"] == f"Host(`{host}`)"
    assert lab[f"traefik.http.services.{router}.loadbalancer.server.port"] == "8080"
    assert env_file(out, env)["ORIGIN"] == f"https://{host}"
    assert subnet in (ROLE / f"deploy_skgym-{env}.yml").read_text()


# --- every knob set --------------------------------------------------------

def test_set_pass_splits_the_router_for_sso_and_keeps_api_outside_it(set_out):
    lab = labels(compose(set_out)["services"]["web"])
    assert lab["traefik.http.routers.skgym.rule"] == "Host(`gym.example.test`) && PathPrefix(`/api/`)"
    assert lab["traefik.http.routers.skgym.middlewares"] == "crowdsec-bouncer@file,default@file"
    assert lab["traefik.http.routers.skgym-ui.rule"] == "Host(`gym.example.test`)"
    assert lab["traefik.http.routers.skgym-ui.middlewares"] == "crowdsec-bouncer@file,default@file,authentik@file"
    assert lab["traefik.http.routers.skgym-ui.service"] == "skgym"
    # TLS_OPTIONS: none omits the label on both routers
    assert not any(k.endswith(".tls.options") for k in lab)


def test_set_pass_pins_data_services_to_the_data_node(set_out):
    svcs = compose(set_out)["services"]
    want = ["node.role == worker", "node.labels.skgym == true", "node.hostname == render-node-1"]
    assert svcs["api"]["deploy"]["placement"]["constraints"] == want
    assert svcs["backup"]["deploy"]["placement"]["constraints"] == want
    assert svcs["web"]["deploy"]["placement"]["constraints"] == ["node.role == manager"]
    assert svcs["web"]["deploy"]["replicas"] == 2
    assert svcs["api"]["volumes"] == ["/var/lib/skgym-render/data:/data"]
    assert (set_out / "_copy/var/lib/skgym-render/data/secret").exists()
    assert svcs["api"]["image"] == SET["API_IMAGE"] and svcs["web"]["image"] == SET["WEB_IMAGE"]
    assert svcs["api"]["deploy"]["resources"]["limits"] == {"cpus": "1.00", "memory": "512M"}
    assert svcs["backup"]["environment"] == {"BACKUP_FREQUENCY": "3600", "BACKUP_KEEP_DAYS": "14",
                                             "BACKUP_START_DELAY": "10"}


def test_set_pass_env_carries_every_setting(set_out):
    env = env_file(set_out)
    assert env["RP_ID"] == "gym.example.test" and env["RP_NAME"] == "Example Gym"
    assert (env["FIRST_USER_ADMIN"], env["INVITE_ONLY"], env["ALLOW_GUEST"], env["PASSWORD_LOGIN"]) == ("0", "1", "0", "1")
    assert env["ADMIN_UIDS"] == "exampleuid1,exampleuid2" and env["DEFAULT_LANG"] == "pt-BR"
    assert (env["AUDIT_LOG"], env["AUDIT_IP"], env["AUDIT_MAX"], env["AUDIT_DAYS"]) == ("0", "net", "0", "30")
    assert (env["MEDIA_UPLOADS"], env["MEDIA_QUOTA_MB"], env["COACH_DISABLED"]) == ("0", "50", "0")
    assert env["VAPID_SUBJECT"] == "mailto:admin@example.test" and env["MEDIA_GC_GRACE_DAYS"] == "30"


# --- knob off ---------------------------------------------------------------

def test_backup_disabled_removes_the_sidecar_only(tmp_path):
    svcs = compose(ok(tmp_path, BASE | {"BACKUP_ENABLED": False}))["services"]
    assert sorted(svcs) == ["api", "web"]


def test_empty_knobs_render_like_unset(tmp_path, base_out):
    empty = {k: "" for k in ("HOSTNAME", "API_IMAGE", "WEB_IMAGE", "DATA_PATH", "BACKUP_PATH", "DATA_NODE",
                             "TRAEFIK_MIDDLEWARES", "SSO_MIDDLEWARE", "TLS_OPTIONS")}
    out = ok(tmp_path, BASE | empty)
    for rel in ("var/data/config/skgym-prod/skgym.yml", "var/data/config/skgym-prod/skgym.env"):
        assert (out / rel).read_text() == (base_out / rel).read_text(), rel


# --- failures ---------------------------------------------------------------

@pytest.mark.parametrize("vault", [
    None,                                                              # no skgym block at all
    {k: v for k, v in BASE.items() if k != "SESSION_SECRET"},          # no secret
    BASE | {"SESSION_SECRET": ""},                                     # empty secret
    BASE | {"SESSION_SECRET": "too-short-secret"},                     # under 32 characters
], ids=["no-block", "missing", "empty", "short"])
def test_missing_or_weak_session_secret_fails_loudly(tmp_path, vault):
    log = fails(tmp_path, vault)
    assert "skgym.SESSION_SECRET is required" in log
    assert "too-short-secret" not in log


@pytest.mark.parametrize("knob", ["API_IMAGE", "WEB_IMAGE"])
def test_unpinned_image_fails(tmp_path, knob):
    log = fails(tmp_path, BASE | {knob: "ghcr.io/duartesantos8/opengym-api:latest"})
    assert "must be pinned by digest" in log


def test_local_data_path_without_data_node_fails(tmp_path):
    log = fails(tmp_path, BASE | {"DATA_PATH": "/srv/skgym/data"})
    assert "DATA_NODE is empty" in log


# --- static ------------------------------------------------------------------

def test_playbook_default_images_equal_the_compose_literals():
    template = (ROLE / "src/config/skgym/skgym.yml.j2").read_text()
    for env in ("dev", "staging", "prod"):
        pb = (ROLE / f"deploy_skgym-{env}.yml").read_text()
        api = re.search(r'skg_default_api_image: "([^"]+)"', pb).group(1)
        web = re.search(r'skg_default_web_image: "([^"]+)"', pb).group(1)
        assert (api, web) == (API_IMAGE, WEB_IMAGE), env
        assert template.count(f"default('{API_IMAGE}', true)") == 2
        assert template.count(f"default('{WEB_IMAGE}', true)") == 1


def test_env_playbooks_differ_only_in_env_and_subnets():
    def norm(text):
        text = re.sub(r"(?<![A-Za-z])(dev|staging|prod)(?![A-Za-z])", "ENV", text)
        return re.sub(r"\d+\.\d+\.\d+\.\d+/\d+", "CIDR", text)
    prod = norm((ROLE / "deploy_skgym-prod.yml").read_text())
    for env in ("dev", "staging"):
        assert norm((ROLE / f"deploy_skgym-{env}.yml").read_text()) == prod, env
