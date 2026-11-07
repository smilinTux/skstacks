"""skpatch-hosts's deploy rendered through REAL Ansible (render/render_playbooks.py)
with a given vault: digest-pinned upstream image, one dashboard replica, the
router built from the framework's host expressions, the middleware and SSO
knobs (forward auth on the whole router, unlike skgym's split), the data
path/data node guard, and loud failures for a missing encryption key or an
unpinned image. ALLOW_ACTIONS is metadata only at this layer (the real
privilege enforcement is provision_skpatch_hosts_nodes.yml, covered in
test_skpatch_hosts_provision.py): here we only check it renders and reports,
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

ROLE = rp.ANSIBLE / "optional" / "skpatch-hosts"
BASE = yaml.safe_load((rp.VARS / "skpatch-hosts.example.yml").read_text())["skpatch_hosts"]
SET = yaml.safe_load((rp.VARS / "skpatch-hosts.set.yml").read_text())["skpatch_hosts"]
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
    doc = {} if vault is None else {"skpatch_hosts": vault}
    (vars_dir / "skpatch-hosts.example.yml").write_text(yaml.safe_dump(doc))
    run_dir = work / f"skpatch-hosts-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "skpatch-hosts" / f"deploy_skpatch-hosts-{env}.yml"
        res = rp.run_one(work, "optional", "skpatch-hosts", env, pb, "base")
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
    return yaml.safe_load((out / f"var/data/config/skpatch-hosts-{env}/skpatch-hosts.yml").read_text())


def labels(svc):
    return dict(l.split("=", 1) for l in svc["deploy"]["labels"])


def env_file(out, env="prod"):
    return dict(l.split("=", 1) for l in (out / f"var/data/config/skpatch-hosts-{env}/skpatch-hosts.env").read_text()
                .splitlines() if l and not l.startswith("#"))


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skpatch-hosts-base"), BASE)


@pytest.fixture(scope="module")
def set_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skpatch-hosts-set"), BASE | SET)


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
    assert dashboard["volumes"] == ["/var/data/skpatch-hosts-prod/data:/data"]
    assert dashboard["networks"] == ["skpatch-hosts-prod", "cloud-public-prod"]
    assert dashboard["healthcheck"]["test"] == ["CMD", "/usr/local/bin/node", "-e", "require('http').get('http://127.0.0.1:3001/',r=>process.exit(r.statusCode<400?0:1)).on('error',()=>process.exit(1))"]
    assert (dashboard["healthcheck"]["interval"], dashboard["healthcheck"]["timeout"],
            dashboard["healthcheck"]["retries"], dashboard["healthcheck"]["start_period"]) == ("30s", "10s", 3, "30s")


def test_default_auth_mode_is_sso_authentik_on_the_whole_router(base_out):
    lab = labels(compose(base_out)["services"]["dashboard"])
    # No API path is carved out: the whole router is behind forward auth.
    assert lab["traefik.http.routers.skpatch-hosts.rule"] == "Host(`skpatch-hosts.render.example.test`)"
    assert lab["traefik.http.routers.skpatch-hosts.middlewares"] == "default-no-error-pages@file,authentik@file"
    assert lab["traefik.http.routers.skpatch-hosts.service"] == "skpatch-hosts"
    assert lab["traefik.http.routers.skpatch-hosts.tls.options"] == "intermediate@file"
    assert not any(k.endswith("-ui.rule") for k in lab)


@pytest.mark.parametrize("mode", ["SSO", "sso", ""])
def test_auth_mode_sso_spellings_render_like_the_default(tmp_path, base_out, mode):
    out = ok(tmp_path, BASE | {"AUTH_MODE": mode})
    rel = "var/data/config/skpatch-hosts-prod/skpatch-hosts.yml"
    assert (out / rel).read_text() == (base_out / rel).read_text()


def test_auth_mode_sso_takes_a_custom_forward_auth_middleware(tmp_path):
    out = ok(tmp_path, BASE | {"AUTH_MODE": "sso", "SSO_MIDDLEWARE": "authentik-other@file"})
    lab = labels(compose(out)["services"]["dashboard"])
    assert lab["traefik.http.routers.skpatch-hosts.middlewares"] == "default-no-error-pages@file,authentik-other@file"


def test_auth_mode_local_is_one_router_without_sso(tmp_path):
    out = ok(tmp_path, BASE | {"AUTH_MODE": "local"})
    lab = labels(compose(out)["services"]["dashboard"])
    assert lab == {
        "com.skt.app": "skpatch-hosts", "com.skt.env": "prod",
        "traefik.enable": "true",
        "traefik.swarm.network": "cloud-public-prod",
        "traefik.http.routers.skpatch-hosts.rule": "Host(`skpatch-hosts.render.example.test`)",
        "traefik.http.routers.skpatch-hosts.entrypoints": "websecure",
        "traefik.http.routers.skpatch-hosts.tls": "true",
        "traefik.http.routers.skpatch-hosts.tls.certresolver": "main",
        "traefik.http.routers.skpatch-hosts.tls.options": "intermediate@file",
        "traefik.http.routers.skpatch-hosts.middlewares": "default-no-error-pages@file",
        "traefik.http.routers.skpatch-hosts.service": "skpatch-hosts",
        "traefik.http.services.skpatch-hosts.loadbalancer.server.port": "3001",
        "traefik.http.services.skpatch-hosts.loadbalancer.healthcheck.path": "/",
        "traefik.http.services.skpatch-hosts.loadbalancer.healthcheck.interval": "30s",
        "traefik.http.services.skpatch-hosts.loadbalancer.healthcheck.timeout": "10s",
    }


# --- AUTH_MODE oidc: the dashboard's own OIDC, no forward auth ------------

OIDC_VAULT = {
    "AUTH_MODE": "oidc",
    "OIDC_ISSUER": "https://sso.example.test/application/o/skpatch-hosts/",
    "OIDC_CLIENT_SECRET": "example-oidc-client-secret-not-a-real-secret-01",
}


def test_auth_mode_oidc_is_one_router_without_sso(tmp_path):
    out = ok(tmp_path, BASE | OIDC_VAULT)
    lab = labels(compose(out)["services"]["dashboard"])
    assert lab["traefik.http.routers.skpatch-hosts.middlewares"] == "default-no-error-pages@file"
    assert "authentik" not in lab["traefik.http.routers.skpatch-hosts.middlewares"]


def test_auth_mode_oidc_renders_the_auth_env(tmp_path):
    out = ok(tmp_path, BASE | OIDC_VAULT)
    env = env_file(out)
    assert env["LUDASH_OIDC_ISSUER"] == OIDC_VAULT["OIDC_ISSUER"]
    assert env["LUDASH_OIDC_CLIENT_ID"] == "skpatch-hosts"
    assert env["LUDASH_OIDC_CLIENT_SECRET"] == OIDC_VAULT["OIDC_CLIENT_SECRET"]
    assert env["LUDASH_OIDC_SCOPE"] == "openid profile email groups"
    assert env["LUDASH_OIDC_GROUPS_CLAIM"] == "groups"
    assert env["LUDASH_OIDC_ADMIN_GROUPS"] == "authentik Admins"
    assert "LUDASH_OIDC_READ_ONLY_GROUPS" not in env
    assert env["LUDASH_OIDC_UNMATCHED_ROLE"] == "deny"
    assert env["LUDASH_OIDC_END_SESSION"] == "true"
    assert env["LUDASH_PASSWORD_LOGIN_ENABLED"] == "false"
    # LUDASH_BASE_URL/LUDASH_TRUST_PROXY are unconditional in every mode.
    assert env["LUDASH_BASE_URL"] == "https://skpatch-hosts.render.example.test"
    assert env["LUDASH_TRUST_PROXY"] == "true"


def test_auth_mode_oidc_client_id_defaults_to_the_same_slug_as_the_portal_tile(tmp_path):
    # Design requirement: the OIDC blueprint and the portal tile must manage
    # the SAME Authentik Application (one object, not two). sksso's own
    # blueprint defaults its client id/slug to the router name too (see
    # test_sksec_web_skpatch_hosts_oidc_sso.py).
    out = ok(tmp_path, BASE | OIDC_VAULT)
    assert env_file(out)["LUDASH_OIDC_CLIENT_ID"] == "skpatch-hosts"
    pb = (ROLE / "deploy_skpatch-hosts-prod.yml").read_text()
    assert 'skp_oidc_client_id: "{{ skpatch_hosts.OIDC_CLIENT_ID | default(skp_router, true) }}"' in pb
    assert 'slug: "{{ skpatch_hosts.AUTHENTIK_TILE_SLUG | default(skp_router, true) }}"' in pb


def test_auth_mode_oidc_custom_scope_and_groups(tmp_path):
    out = ok(tmp_path, BASE | OIDC_VAULT | {
        "OIDC_SCOPE": "openid profile email groups offline_access",
        "OIDC_GROUPS_CLAIM": "roles",
        "OIDC_ADMIN_GROUPS": "skpatch-hosts-admins",
        "OIDC_READ_ONLY_GROUPS": "skpatch-hosts-viewers",
        "OIDC_UNMATCHED_ROLE": "read-only",
        "OIDC_END_SESSION": False,
        "OIDC_CLIENT_ID": "skpatch-hosts-custom",
    })
    env = env_file(out)
    assert env["LUDASH_OIDC_SCOPE"] == "openid profile email groups offline_access"
    assert env["LUDASH_OIDC_GROUPS_CLAIM"] == "roles"
    assert env["LUDASH_OIDC_ADMIN_GROUPS"] == "skpatch-hosts-admins"
    assert env["LUDASH_OIDC_READ_ONLY_GROUPS"] == "skpatch-hosts-viewers"
    assert env["LUDASH_OIDC_UNMATCHED_ROLE"] == "read-only"
    assert env["LUDASH_OIDC_END_SESSION"] == "false"
    assert env["LUDASH_OIDC_CLIENT_ID"] == "skpatch-hosts-custom"


def test_auth_mode_oidc_refuses_missing_issuer(tmp_path):
    log = fails(tmp_path, BASE | {k: v for k, v in OIDC_VAULT.items() if k != "OIDC_ISSUER"})
    assert "needs OIDC_ISSUER" in log


def test_auth_mode_oidc_refuses_short_client_secret(tmp_path):
    log = fails(tmp_path, BASE | OIDC_VAULT | {"OIDC_CLIENT_SECRET": "too-short"})
    assert "OIDC_CLIENT_SECRET" in log
    assert "too-short" not in log


def test_auth_mode_oidc_refuses_an_sso_middleware(tmp_path):
    log = fails(tmp_path, BASE | OIDC_VAULT | {"SSO_MIDDLEWARE": "authentik@file"})
    assert "AUTH_MODE is 'oidc'" in log and "SSO_MIDDLEWARE is set" in log


def test_auth_mode_oidc_refuses_bad_unmatched_role(tmp_path):
    log = fails(tmp_path, BASE | OIDC_VAULT | {"OIDC_UNMATCHED_ROLE": "superadmin"})
    assert "OIDC_UNMATCHED_ROLE must be deny" in log


def test_default_env_binds_the_host_and_trusts_the_proxy(base_out):
    assert env_file(base_out) == {
        "LUDASH_ENCRYPTION_KEY": "example-ludash-key-not-a-secret-0123456789AB",
        "LUDASH_DB_PATH": "/data/dashboard.db",
        "LUDASH_BASE_URL": "https://skpatch-hosts.render.example.test",
        "LUDASH_PORT": "3001", "LUDASH_HOST": "0.0.0.0", "LUDASH_TRUST_PROXY": "true",
        "LUDASH_LOG_LEVEL": "info", "TZ": "UTC", "NODE_ENV": "production",
    }


def test_encryption_key_lands_only_in_the_root_only_env_file(base_out):
    secret = BASE["LUDASH_ENCRYPTION_KEY"]
    pb = (ROLE / "deploy_skpatch-hosts-prod.yml").read_text()
    for name in ("app env (root only)",):
        block = pb.split(name)[1].split("- name:")[0]
        assert "mode: '0600'" in block, name


@pytest.mark.parametrize("env,host,router,subnet", [
    ("dev", "skpatch-hosts-dev.render.example.test", "skpatch-hosts-dev", "172.16.156.0/24"),
    ("staging", "skpatch-hosts-staging.render.example.test", "skpatch-hosts-staging", "172.16.155.0/24"),
])
def test_dev_and_staging_hosts_routers_and_subnets(tmp_path, env, host, router, subnet):
    out = ok(tmp_path, BASE, env)
    lab = labels(compose(out, env)["services"]["dashboard"])
    assert lab[f"traefik.http.routers.{router}.rule"] == f"Host(`{host}`)"
    assert env_file(out, env)["LUDASH_BASE_URL"] == f"https://{host}"
    assert subnet in (ROLE / f"deploy_skpatch-hosts-{env}.yml").read_text()


# --- ALLOW_ACTIONS: metadata only at the deploy layer -----------------------

@pytest.mark.parametrize("value,word", [(False, "REPORT-ONLY"), (True, "ALLOWED")])
def test_allow_actions_only_changes_the_report_line(tmp_path, base_out, value, word):
    out = ok(tmp_path, BASE | {"ALLOW_ACTIONS": value})
    rel = "var/data/config/skpatch-hosts-prod/skpatch-hosts.yml"
    # The compose output (what actually ships) does not change at all: this
    # knob grants nothing at the swarm-deploy layer, by design.
    assert (out / rel).read_text() == (base_out / rel).read_text()
    pb = (ROLE / "deploy_skpatch-hosts-prod.yml").read_text()
    assert word in pb


# --- every knob set --------------------------------------------------------

def test_set_pass_pins_dashboard_to_the_data_node(set_out):
    svcs = compose(set_out)["services"]
    want = ["node.role == worker", "node.labels.skpatch-hosts == true", "node.hostname == render-node-1"]
    assert svcs["dashboard"]["deploy"]["placement"]["constraints"] == want
    assert svcs["dashboard"]["volumes"] == ["/var/lib/skpatch-hosts-render/data:/data"]
    assert svcs["dashboard"]["image"] == SET["IMAGE"]
    assert svcs["dashboard"]["deploy"]["resources"]["limits"] == {"cpus": "1.00", "memory": "768M"}


def test_set_pass_env_carries_every_setting(set_out):
    env = env_file(set_out)
    assert env["LUDASH_LOG_LEVEL"] == "debug" and env["TZ"] == "Europe/Zurich"
    assert env["SOME_UPSTREAM_VAR"] == "example-value"


def test_set_pass_splits_middlewares_and_omits_tls_options(set_out):
    lab = labels(compose(set_out)["services"]["dashboard"])
    assert lab["traefik.http.routers.skpatch-hosts.middlewares"] == "crowdsec-bouncer@file,default-no-error-pages@file,authentik@file"
    assert not any(k.endswith(".tls.options") for k in lab)


# --- failures ---------------------------------------------------------------

@pytest.mark.parametrize("vault", [
    None,                                                                   # no skpatch_hosts block at all
    {k: v for k, v in BASE.items() if k != "LUDASH_ENCRYPTION_KEY"},        # no secret
    BASE | {"LUDASH_ENCRYPTION_KEY": ""},                                   # empty secret
    BASE | {"LUDASH_ENCRYPTION_KEY": "too-short-key"},                      # under 32 characters
], ids=["no-block", "missing", "empty", "short"])
def test_missing_or_weak_encryption_key_fails_loudly(tmp_path, vault):
    log = fails(tmp_path, vault)
    assert "skpatch_hosts.LUDASH_ENCRYPTION_KEY is required" in log
    assert "too-short-key" not in log


def test_unpinned_image_fails(tmp_path):
    log = fails(tmp_path, BASE | {"IMAGE": "ghcr.io/theduffman85/linux-update-dashboard:latest"})
    assert "must be pinned by digest" in log


@pytest.mark.parametrize("mode", ["authentik", "off", "none", "bogus"])
def test_unknown_auth_mode_fails(tmp_path, mode):
    log = fails(tmp_path, BASE | {"AUTH_MODE": mode})
    assert "skpatch_hosts.AUTH_MODE must be 'sso', 'local' or 'oidc'" in log


def test_auth_mode_local_refuses_an_sso_middleware(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "local", "SSO_MIDDLEWARE": "authentik@file"})
    assert "AUTH_MODE is 'local'" in log and "SSO_MIDDLEWARE" in log


@pytest.mark.parametrize("mode", ["sso", "local"])
def test_forward_auth_in_traefik_middlewares_is_refused(tmp_path, mode):
    log = fails(tmp_path, BASE | {"AUTH_MODE": mode, "TRAEFIK_MIDDLEWARES": "default@file,authentik@file"})
    assert "TRAEFIK_MIDDLEWARES" in log and "forward auth" in log


def test_local_data_path_without_data_node_fails(tmp_path):
    log = fails(tmp_path, BASE | {"DATA_PATH": "/srv/skpatch-hosts/data"})
    assert "DATA_NODE is empty" in log


# --- static ------------------------------------------------------------------

def test_playbook_default_image_equals_the_compose_literal():
    template = (ROLE / "src/config/skpatch-hosts/skpatch-hosts.yml.j2").read_text()
    for env in ("dev", "staging", "prod"):
        pb = (ROLE / f"deploy_skpatch-hosts-{env}.yml").read_text()
        default = re.search(r'skp_default_image: "([^"]+)"', pb).group(1)
        assert default == IMAGE, env
        assert template.count(f"default('{IMAGE}', true)") == 1


def test_env_playbooks_differ_only_in_env_and_subnets():
    def norm(text):
        text = re.sub(r"(?<![A-Za-z])(dev|staging|prod)(?![A-Za-z])", "ENV", text)
        return re.sub(r"\d+\.\d+\.\d+\.\d+/\d+", "CIDR", text)
    prod = norm((ROLE / "deploy_skpatch-hosts-prod.yml").read_text())
    for env in ("dev", "staging"):
        assert norm((ROLE / f"deploy_skpatch-hosts-{env}.yml").read_text()) == prod, env



# --- the healthcheck never assumes a binary the pinned image may lack ------

def test_dashboard_healthcheck_does_not_use_wget_or_curl(base_out):
    # Regression guard for the bug found live 2026-10-27: same publisher/
    # base image as sksec-web's crowdsec-web-ui (Debian slim, no wget or
    # curl), so a healthcheck test calling either one always fails -- the
    # app itself was healthy and running the whole time, but Swarm killed
    # and replaced the task every ~65-70s forever on the failing probe.
    # node is guaranteed present (it runs the app); the healthcheck must
    # use it directly rather than assume a shell utility.
    test = compose(base_out)["services"]["dashboard"]["healthcheck"]["test"]
    joined = " ".join(test)
    assert "wget" not in joined and "curl" not in joined, joined
    assert "node" in joined
