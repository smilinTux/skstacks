"""skrac's deploy rendered through REAL Ansible (render/render_playbooks.py)
with a given vault: the digest-pinned MeshCentral image, one server
replica, the router split between the agent/installer paths and
everything else (and that the SSO middleware never reaches the agent
router), the reverse-proxy settings in config.json (tlsOffload/
trustedProxy/aliasPort/certUrl), the OIDC knobs, the data path / data node
guard, and loud failures for a missing OIDC secret or an unpinned image."""
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "skrac"
BASE = yaml.safe_load((rp.VARS / "skrac.example.yml").read_text())["skrac"]
SET = yaml.safe_load((rp.VARS / "skrac.set.yml").read_text())["skrac"]
IMAGE = ("ghcr.io/ylianst/meshcentral:1.2.6@sha256:"
         "09c5abfcf0758f2a7ab9b43fa8acb838b92bba1498c1447fc8c09bb1d296b73e")


def render(tmp: Path, vault, env: str = "prod") -> tuple[dict, Path]:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    doc = {} if vault is None else {"skrac": vault}
    (vars_dir / "skrac.example.yml").write_text(yaml.safe_dump(doc))
    run_dir = work / f"skrac-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "skrac" / f"deploy_skrac-{env}.yml"
        res = rp.run_one(work, "optional", "skrac", env, pb, "base")
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
    return yaml.safe_load((out / f"var/data/config/skrac-{env}/skrac.yml").read_text())


def labels(svc):
    return dict(l.split("=", 1) for l in svc["deploy"]["labels"])


def config_json(out, env="prod", data_path=None):
    import json
    rel = (data_path or f"/var/data/skrac-{env}/data").lstrip("/")
    return json.loads((out / rel / "config.json").read_text())


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skrac-base"), BASE)


@pytest.fixture(scope="module")
def set_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skrac-set"), BASE | SET)


# --- happy path: defaults --------------------------------------------------

def test_default_services_and_single_server_replica(base_out):
    svcs = compose(base_out)["services"]
    assert sorted(svcs) == ["backup", "server"]
    server, backup = svcs["server"], svcs["backup"]
    assert server["image"] == IMAGE and backup["image"] == IMAGE
    assert server["deploy"]["replicas"] == 1 and server["deploy"]["update_config"]["order"] == "stop-first"
    assert backup["deploy"]["replicas"] == 1
    for svc in (server, backup):
        assert svc["deploy"]["restart_policy"]["condition"] == "any"
        assert svc["deploy"]["placement"]["constraints"] == ["node.role == worker"]
    assert server["volumes"] == [
        "/var/data/skrac-prod/data:/opt/meshcentral/meshcentral-data",
        "/var/data/skrac-prod/files:/opt/meshcentral/meshcentral-files",
    ]
    assert backup["volumes"] == [
        "/var/data/skrac-prod/data:/opt/meshcentral/meshcentral-data:ro",
        "/var/data/skrac-prod/files:/opt/meshcentral/meshcentral-files:ro",
        "/var/data/skrac-prod/backup:/backup",
        "/var/data/skrac-prod/bin/skrac-backup:/usr/local/bin/skrac-backup:ro",
    ]
    assert server["networks"] == ["skrac-prod", "cloud-public-prod"]
    assert backup["healthcheck"] == {"disable": True}
    assert backup["entrypoint"] == ["/bin/sh", "/usr/local/bin/skrac-backup"] and backup["command"] == ["loop"]
    assert "extra_hosts" not in server and "environment" not in server
    assert not (base_out / "_copy/var/data/config/skrac-prod/extra-ca.pem").exists()


def test_default_router_splits_agent_paths_from_everything_else(base_out):
    lab = labels(compose(base_out)["services"]["server"])
    assert lab["traefik.http.routers.skrac-agent.rule"] == (
        "Host(`skrac.render.example.test`) && (PathPrefix(`/agent.ashx`) || "
        "PathPrefix(`/meshrelay.ashx`) || PathPrefix(`/meshagents`) || "
        "PathPrefix(`/meshsettings.ashx`) || PathPrefix(`/devicefile.ashx`) || "
        "PathPrefix(`/health.ashx`))")
    assert lab["traefik.http.routers.skrac-agent.priority"] == "100"
    assert lab["traefik.http.routers.skrac-agent.middlewares"] == "default-no-crowdsec@file"
    assert lab["traefik.http.routers.skrac.rule"] == "Host(`skrac.render.example.test`)"
    assert lab["traefik.http.routers.skrac.priority"] == "1"
    assert lab["traefik.http.routers.skrac.middlewares"] == "default-no-crowdsec@file"
    assert lab["traefik.http.services.skrac.loadbalancer.server.port"] == "4430"
    assert lab["traefik.http.services.skrac.loadbalancer.server.scheme"] == "http"
    assert lab["traefik.http.services.skrac.loadbalancer.healthcheck.path"] == "/health.ashx"


def test_default_config_json_reverse_proxy_settings(base_out):
    cfg = config_json(base_out)
    assert cfg["settings"]["port"] == 4430
    assert cfg["settings"]["aliasPort"] == 443
    assert cfg["settings"]["tlsOffload"] == "172.16.200.0/24"
    assert cfg["settings"]["trustedProxy"] == "172.16.200.0/24"
    dom = cfg["domains"][""]
    assert dom["certUrl"] == "https://skrac.render.example.test/"
    assert dom["showPasswordLogin"] is True
    assert dom["newAccounts"] is False
    assert dom["authStrategies"] == {}
    assert "unknownUserRootRedirect" not in dom


def test_backup_script_is_copied_verbatim(base_out):
    copied = base_out / "_copy/var/data/skrac-prod/bin/skrac-backup"
    assert copied.read_text() == (ROLE / "src/skrac/skrac-backup").read_text()


def test_config_json_is_root_only_and_never_logged(base_out):
    pb = (ROLE / "deploy_skrac-prod.yml").read_text()
    block = pb.split("Write the MeshCentral config")[1].split("- name:")[0]
    assert "mode: '0600'" in block and "no_log: true" in block


@pytest.mark.parametrize("env,host,router,subnet", [
    ("dev", "skrac-dev.render.example.test", "skrac-dev", "172.16.245.0/24"),
    ("staging", "skrac-staging.render.example.test", "skrac-staging", "172.16.239.0/24"),
])
def test_dev_and_staging_hosts_routers_and_subnets(tmp_path, env, host, router, subnet):
    out = ok(tmp_path, BASE, env)
    server = compose(out, env)["services"]["server"]
    lab = labels(server)
    assert lab[f"traefik.http.routers.{router}.rule"] == f"Host(`{host}`)"
    assert subnet in (ROLE / f"deploy_skrac-{env}.yml").read_text()


# --- every knob set, oidc mode ---------------------------------------------

def test_set_pass_oidc_strategy_and_group_claim(set_out):
    cfg = config_json(set_out, data_path=SET["DATA_PATH"])
    dom = cfg["domains"][""]
    oidc = dom["authStrategies"]["oidc"]
    assert oidc["issuer"] == "https://sso.example.test/application/o/skrac/"
    assert oidc["client"] == {"client_id": "example-oidc-client-id",
                               "client_secret": "example-oidc-client-secret-0123456789"}
    assert oidc["groups"] == {"claim": "groups", "siteadmin": ["skadmin"]}
    assert oidc["newAccounts"] is True
    assert dom["showPasswordLogin"] is False  # LOCAL_LOGIN_HIDDEN: true
    assert dom["unknownUserRootRedirect"] == "auth-oidc"
    assert cfg["settings"]["wsCompression"] is True  # EXTRA_CONFIG merge


def test_set_pass_reaches_the_issuer_by_sso_host_ip_and_trusts_the_extra_ca(set_out):
    server = compose(set_out)["services"]["server"]
    assert server["extra_hosts"] == ["sso.example.test:192.0.2.10"]
    assert server["environment"] == {"NODE_EXTRA_CA_CERTS": "/opt/meshcentral/extra-ca.pem"}
    assert "/var/data/config/skrac-prod/extra-ca.pem:/opt/meshcentral/extra-ca.pem:ro" in server["volumes"]
    assert (set_out / "_copy/var/data/config/skrac-prod/extra-ca.pem").read_text() == SET["EXTRA_CA_CERT"]


def test_extra_ca_alone_is_fine_in_local_mode(tmp_path):
    server = compose(ok(tmp_path, BASE | {"EXTRA_CA_CERT": SET["EXTRA_CA_CERT"]}))["services"]["server"]
    assert server["environment"] == {"NODE_EXTRA_CA_CERTS": "/opt/meshcentral/extra-ca.pem"}
    assert "extra_hosts" not in server


def test_bad_sso_host_ip_fails(tmp_path):
    vault = BASE | {k: SET[k] for k in ("AUTH_MODE", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET")}
    log = fails(tmp_path, vault | {"SSO_HOST_IP": "sso.example.test; rm -rf /"})
    assert "SSO_HOST_IP, when set, a plain IP address" in log


def test_set_pass_splits_the_router_and_sso_never_reaches_agent(set_out):
    lab = labels(compose(set_out)["services"]["server"])
    assert lab["traefik.http.routers.skrac.middlewares"] == "crowdsec-bouncer@file,default@file,authentik@file"
    assert lab["traefik.http.routers.skrac-agent.middlewares"] == "default@file"
    assert "authentik@file" not in lab["traefik.http.routers.skrac-agent.middlewares"]
    assert not any(k.endswith(".tls.options") for k in lab)  # TLS_OPTIONS: none


def test_set_pass_pins_data_services_to_the_data_node(set_out):
    svcs = compose(set_out)["services"]
    want = ["node.role == worker", "node.labels.skrac == true", "node.hostname == render-node-1"]
    assert svcs["server"]["deploy"]["placement"]["constraints"] == want
    assert svcs["backup"]["deploy"]["placement"]["constraints"] == want
    assert svcs["server"]["volumes"][0] == "/var/lib/skrac-render/data:/opt/meshcentral/meshcentral-data"
    assert svcs["server"]["image"] == SET["MESHCENTRAL_IMAGE"]
    assert svcs["server"]["deploy"]["resources"]["limits"] == {"cpus": "2.00", "memory": "1536M"}
    assert svcs["backup"]["environment"]["SKRAC_BACKUP_INCLUDE_FILES"] == "1"


# --- knob off ---------------------------------------------------------------

def test_backup_disabled_removes_the_sidecar_only(tmp_path):
    svcs = compose(ok(tmp_path, BASE | {"BACKUP_ENABLED": False}))["services"]
    assert sorted(svcs) == ["server"]


def test_empty_knobs_render_like_unset(tmp_path, base_out):
    empty = {k: "" for k in ("HOSTNAME", "MESHCENTRAL_IMAGE", "DATA_PATH", "FILES_PATH", "BACKUP_PATH",
                             "DATA_NODE", "TRAEFIK_MIDDLEWARES", "AGENT_TRAEFIK_MIDDLEWARES",
                             "SSO_MIDDLEWARE", "TLS_OPTIONS", "SSO_HOST_IP", "EXTRA_CA_CERT")}
    out = ok(tmp_path, BASE | empty)
    assert (out / "var/data/config/skrac-prod/skrac.yml").read_text() == \
        (base_out / "var/data/config/skrac-prod/skrac.yml").read_text()


def test_auth_mode_local_omits_oidc_strategy(base_out):
    cfg = config_json(base_out)
    assert cfg["domains"][""]["authStrategies"] == {}


# --- failures ---------------------------------------------------------------

@pytest.mark.parametrize("vault", [
    BASE | {"AUTH_MODE": "oidc"},                                                      # nothing else set
    BASE | {"AUTH_MODE": "oidc", "OIDC_ISSUER": "https://sso.example.test/application/o/skrac/",
            "OIDC_CLIENT_ID": "cid"},                                                   # no secret
    BASE | {"AUTH_MODE": "oidc", "OIDC_ISSUER": "https://sso.example.test/application/o/skrac/",
            "OIDC_CLIENT_ID": "cid", "OIDC_CLIENT_SECRET": "too-short"},                # under 16 chars
], ids=["nothing-set", "missing-secret", "short-secret"])
def test_oidc_mode_without_a_full_authentik_config_fails_loudly(tmp_path, vault):
    log = fails(tmp_path, vault)
    assert "OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET" in log
    assert "too-short" not in log


# AUTH_MODE local is the standalone path: a vault that also asks for SSO
# contradicts itself and must be refused, naming the conflicting keys.
SECRET = "example-oidc-client-secret-0123456789"


@pytest.mark.parametrize("extra,keys", [
    ({"AUTHENTIK_PROVISION": True}, ["AUTHENTIK_PROVISION"]),
    ({"SSO_MIDDLEWARE": "authentik@file"}, ["SSO_MIDDLEWARE"]),
    ({"SSO_HOST_IP": "192.0.2.10"}, ["SSO_HOST_IP"]),
    ({"OIDC_AUTO_REDIRECT": True}, ["OIDC_AUTO_REDIRECT"]),
    ({"LOCAL_LOGIN_HIDDEN": True}, ["LOCAL_LOGIN_HIDDEN"]),
    ({"OIDC_ISSUER": "https://sso.example.test/application/o/skrac/"}, ["OIDC_ISSUER"]),
    ({"OIDC_CLIENT_ID": "cid"}, ["OIDC_CLIENT_ID"]),
    ({"OIDC_CLIENT_SECRET": SECRET}, ["OIDC_CLIENT_SECRET"]),
    ({"AUTHENTIK_PROVISION": True, "SSO_MIDDLEWARE": "authentik@file"},
     ["AUTHENTIK_PROVISION", "SSO_MIDDLEWARE"]),
], ids=["provision", "forward-auth", "sso-host-ip", "auto-redirect", "login-hidden", "issuer", "client-id",
        "client-secret", "provision+forward-auth"])
@pytest.mark.parametrize("explicit", [True, False], ids=["explicit-local", "default-local"])
def test_local_mode_refuses_sso_settings_and_names_the_keys(tmp_path, extra, keys, explicit):
    vault = BASE | extra | ({"AUTH_MODE": "local"} if explicit else {})
    log = fails(tmp_path, vault)
    assert "AUTH_MODE is 'local'" in log
    assert "sets " + ", ".join(keys) + "," in log
    assert SECRET not in log


def test_local_mode_accepts_sso_keys_that_are_empty_or_off(tmp_path, base_out):
    off = {"AUTH_MODE": "local", "AUTHENTIK_PROVISION": False, "SSO_MIDDLEWARE": "", "SSO_HOST_IP": "",
           "OIDC_AUTO_REDIRECT": False, "LOCAL_LOGIN_HIDDEN": False,
           "OIDC_ISSUER": "", "OIDC_CLIENT_ID": "", "OIDC_CLIENT_SECRET": ""}
    out = ok(tmp_path, BASE | off)
    assert (out / "var/data/config/skrac-prod/skrac.yml").read_text() == \
        (base_out / "var/data/config/skrac-prod/skrac.yml").read_text()
    assert config_json(out) == config_json(base_out)


def test_oidc_mode_still_allows_forward_auth_and_hidden_password_form(set_out):
    # The set pass is oidc + SSO_MIDDLEWARE + LOCAL_LOGIN_HIDDEN +
    # OIDC_AUTO_REDIRECT: none of those are conflicts in oidc mode.
    lab = labels(compose(set_out)["services"]["server"])
    assert lab["traefik.http.routers.skrac.middlewares"].endswith(",authentik@file")


def test_bad_auth_mode_fails(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "saml"})
    assert "must be 'local' or 'oidc'" in log


def test_unpinned_image_fails(tmp_path):
    log = fails(tmp_path, BASE | {"MESHCENTRAL_IMAGE": "ghcr.io/ylianst/meshcentral:latest"})
    assert "must be pinned by digest" in log


def test_local_data_path_without_data_node_fails(tmp_path):
    log = fails(tmp_path, BASE | {"DATA_PATH": "/srv/skrac/data"})
    assert "DATA_NODE is empty" in log


# --- static ------------------------------------------------------------------

def test_playbook_default_image_equals_the_compose_literal():
    template = (ROLE / "src/config/skrac/skrac.yml.j2").read_text()
    for env in ("dev", "staging", "prod"):
        pb = (ROLE / f"deploy_skrac-{env}.yml").read_text()
        image = re.search(r'skr_default_image: "([^"]+)"', pb).group(1)
        assert image == IMAGE, env
        assert template.count(f"default('{IMAGE}', true)") == 2


def test_env_playbooks_differ_only_in_env_and_subnets():
    def norm(text):
        text = re.sub(r"(?<![A-Za-z])(dev|staging|prod)(?![A-Za-z])", "ENV", text)
        return re.sub(r"\d+\.\d+\.\d+\.\d+/\d+", "CIDR", text)
    prod = norm((ROLE / "deploy_skrac-prod.yml").read_text())
    for env in ("dev", "staging"):
        assert norm((ROLE / f"deploy_skrac-{env}.yml").read_text()) == prod, env
