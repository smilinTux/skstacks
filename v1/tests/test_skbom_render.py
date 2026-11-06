"""skbom's deploy rendered through REAL Ansible (render/render_playbooks.py)
with a given vault: the two digest-pinned Dependency-Track images plus the
digest-pinned Postgres pair (the same literals skflow defaults to), the
Traefik router split (PathPrefix(`/api`) to apiserver, everything else to
frontend, each with its own loadbalancer service), OIDC off by default and
refused when turned on without both an issuer and a client id, the
PLACEMENT_CONSTRAINTS default of node.labels.skbom == true (not the bare
node.role == worker), the three-path DATA_NODE guard, and loud failures for
a missing/short DB_PASSWORD and an unpinned image."""
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "skbom"
BASE = yaml.safe_load((rp.VARS / "skbom.example.yml").read_text())["skbom"]
SET = yaml.safe_load((rp.VARS / "skbom.set.yml").read_text())["skbom"]
APISERVER_IMAGE = ("dependencytrack/apiserver:4.14.6@sha256:"
                   "3110dd66ee9d1b4b9a4dc6c51c68c23f14ba006b6e5a74d0b5204e169a1da09a")
FRONTEND_IMAGE = ("dependencytrack/frontend:4.14.6@sha256:"
                  "f298c3e6061096bf76575a1bbe994528089141856ec7b2ec63469cf3f2abeedf")
PG_IMAGE = ("postgres:17.7-alpine@sha256:"
            "bb377b7239d2774ac8cc76f481596ce96c5a6b5e9d141f6d0a0ee371a6e7c0f2")
PG_BACKUP_IMAGE = ("postgres:17.7@sha256:"
                   "2006493727bd5277eece187319af1ef82b4cf82cf4fc1ed00da0775b646ac2a4")


def render(tmp: Path, vault, env: str = "prod") -> tuple[dict, Path]:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    doc = {} if vault is None else {"skbom": vault}
    (vars_dir / "skbom.example.yml").write_text(yaml.safe_dump(doc))
    run_dir = work / f"skbom-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "skbom" / f"deploy_skbom-{env}.yml"
        res = rp.run_one(work, "optional", "skbom", env, pb, "base")
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
    return yaml.safe_load((out / f"var/data/config/skbom-{env}/skbom.yml").read_text())


def envfile(out, name="apiserver.env", env="prod"):
    lines = (out / f"var/data/config/skbom-{env}/{name}").read_text().splitlines()
    return dict(l.split("=", 1) for l in lines if l and not l.startswith("#"))


def labels(svc):
    return dict(l.split("=", 1) for l in svc["deploy"]["labels"])


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skbom-base"), BASE)


@pytest.fixture(scope="module")
def set_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skbom-set"), BASE | SET)


# --- happy path: defaults --------------------------------------------------

def test_default_services_and_images(base_out):
    svcs = compose(base_out)["services"]
    assert sorted(svcs) == ["apiserver", "frontend", "postgres", "postgres-db-backup"]
    assert svcs["apiserver"]["image"] == APISERVER_IMAGE
    assert svcs["frontend"]["image"] == FRONTEND_IMAGE
    assert svcs["postgres"]["image"] == PG_IMAGE
    assert svcs["postgres-db-backup"]["image"] == PG_BACKUP_IMAGE
    assert svcs["apiserver"]["deploy"]["replicas"] == 1
    assert svcs["frontend"]["deploy"]["replicas"] == 1
    assert svcs["postgres"]["deploy"]["replicas"] == 1
    assert svcs["postgres"]["deploy"]["update_config"]["order"] == "stop-first"
    for svc in svcs.values():
        assert svc["deploy"]["restart_policy"]["condition"] == "any"
        # The default placement constraint is the node LABEL, never the
        # bare node.role == worker most other optional roles default to.
        assert svc["deploy"]["placement"]["constraints"] == ["node.labels.skbom == true"]
    assert svcs["apiserver"]["volumes"] == ["/var/data/skbom-prod/apiserver-data:/data"]
    assert svcs["postgres"]["volumes"] == ["/var/data/runtime/skbom-prod/postgres:/var/lib/postgresql/data"]
    assert svcs["postgres-db-backup"]["volumes"] == ["/var/data/skbom-prod/database-dump:/dump"]
    assert "pg_dumpall" in svcs["postgres-db-backup"]["entrypoint"][2]
    assert "8080/health" in svcs["apiserver"]["healthcheck"]["test"][1]
    assert svcs["frontend"]["healthcheck"]["test"] == ["CMD", "wget", "--spider", "-q", "http://127.0.0.1:8080/"]


def test_default_apiserver_env_points_at_the_stack_postgres(base_out):
    e = envfile(base_out)
    assert e["ALPINE_DATABASE_MODE"] == "external"
    assert e["ALPINE_DATABASE_URL"] == "jdbc:postgresql://postgres:5432/dtrack"
    assert e["ALPINE_DATABASE_DRIVER"] == "org.postgresql.Driver"
    assert e["ALPINE_DATABASE_USERNAME"] == "dtrack"
    assert e["ALPINE_DATABASE_PASSWORD"] == BASE["DB_PASSWORD"]
    assert e["ALPINE_METRICS_ENABLED"] == "true"
    assert e["EXTRA_JAVA_OPTIONS"] == "-Xmx3g"
    assert "ALPINE_OIDC_ENABLED" not in e
    assert envfile(base_out, "postgres.env")["POSTGRES_PASSWORD"] == BASE["DB_PASSWORD"]
    assert envfile(base_out, "postgres-backup.env")["PGPASSWORD"] == BASE["DB_PASSWORD"]


def test_default_frontend_env_has_no_oidc(base_out):
    e = envfile(base_out, "frontend.env")
    assert e["API_BASE_URL"] == "https://skbom.render.example.test"
    assert "OIDC_ISSUER" not in e and "OIDC_CLIENT_ID" not in e


def test_default_router_split(base_out):
    svcs = compose(base_out)["services"]
    api_lab = labels(svcs["apiserver"])
    fe_lab = labels(svcs["frontend"])
    assert api_lab["traefik.http.routers.skbom-api.rule"] == "Host(`skbom.render.example.test`) && PathPrefix(`/api`)"
    assert api_lab["traefik.http.routers.skbom-api.service"] == "skbom-api"
    assert api_lab["traefik.http.services.skbom-api.loadbalancer.server.port"] == "8080"
    assert api_lab["traefik.http.routers.skbom-api.priority"] == "50"
    assert fe_lab["traefik.http.routers.skbom.rule"] == "Host(`skbom.render.example.test`)"
    assert fe_lab["traefik.http.routers.skbom.service"] == "skbom"
    assert fe_lab["traefik.http.services.skbom.loadbalancer.server.port"] == "8080"
    assert fe_lab["traefik.http.routers.skbom.priority"] == "5"
    assert api_lab["traefik.http.routers.skbom-api.middlewares"] == "default-no-error-pages@file"
    assert fe_lab["traefik.http.routers.skbom.middlewares"] == "default-no-error-pages@file"


def test_env_files_are_root_only_and_never_logged():
    pb = (ROLE / "deploy_skbom-prod.yml").read_text()
    block = pb.split("Process {{ app }} environment templates")[1].split("- name:")[0]
    assert "mode: '0600'" in block and "no_log: true" in block


@pytest.mark.parametrize("env,host,router,subnet", [
    ("dev", "skbom-dev.render.example.test", "skbom-dev", "172.16.66.0/24"),
    ("staging", "skbom-staging.render.example.test", "skbom-staging", "172.16.65.0/24"),
])
def test_dev_and_staging_hosts_routers_and_subnets(tmp_path, env, host, router, subnet):
    out = ok(tmp_path, BASE, env)
    lab = labels(compose(out, env)["services"]["frontend"])
    assert lab[f"traefik.http.routers.{router}.rule"] == f"Host(`{host}`)"
    assert subnet in (ROLE / f"deploy_skbom-{env}.yml").read_text()


# --- every knob set: images, sizing, OIDC ----------------------------------

def test_set_pass_images_and_db_identity(set_out):
    svcs = compose(set_out)["services"]
    assert svcs["apiserver"]["image"] == SET["APISERVER_IMAGE"]
    assert svcs["frontend"]["image"] == SET["FRONTEND_IMAGE"]
    assert svcs["postgres"]["image"] == SET["POSTGRES_IMAGE"]
    assert svcs["postgres-db-backup"]["image"] == SET["POSTGRES_BACKUP_IMAGE"]
    e = envfile(set_out)
    assert e["ALPINE_DATABASE_URL"] == "jdbc:postgresql://postgres:5432/dtrack-example-db"
    assert e["ALPINE_DATABASE_USERNAME"] == "dtrack-example"
    assert e["EXTRA_JAVA_OPTIONS"] == "-Xmx6g" and e["LOGGING_LEVEL"] == "DEBUG"
    assert e["SOME_UPSTREAM_VAR"] == "example-value"


def test_set_pass_oidc_on_both_sides(set_out):
    api = envfile(set_out, "apiserver.env")
    fe = envfile(set_out, "frontend.env")
    assert api["ALPINE_OIDC_ENABLED"] == "true"
    assert api["ALPINE_OIDC_ISSUER"] == SET["OIDC_ISSUER"] == fe["OIDC_ISSUER"]
    assert api["ALPINE_OIDC_CLIENT_ID"] == SET["OIDC_CLIENT_ID"] == fe["OIDC_CLIENT_ID"]
    assert api["ALPINE_OIDC_USERNAME_CLAIM"] == "email"
    assert api["ALPINE_OIDC_TEAMS_CLAIM"] == "dt-teams"
    assert api["ALPINE_OIDC_USER_PROVISIONING"] == "false"
    assert api["ALPINE_OIDC_TEAM_SYNCHRONIZATION"] == "true"
    assert fe["OIDC_LOGIN_BUTTON_TEXT"] == "Sign in with Example SSO"


def test_set_pass_sizing_and_replicas(set_out):
    svcs = compose(set_out)["services"]
    assert svcs["apiserver"]["deploy"]["resources"]["limits"] == {"cpus": "4.00", "memory": "6144M"}
    assert svcs["frontend"]["deploy"]["replicas"] == 2
    assert svcs["postgres"]["deploy"]["resources"]["limits"]["memory"] == "2048M"


def test_set_pass_data_node_pins_only_the_three_data_services(set_out):
    svcs = compose(set_out)["services"]
    want = ["node.role == worker", "node.labels.skbom == true", "node.hostname == render-node-1"]
    assert svcs["apiserver"]["deploy"]["placement"]["constraints"] == want
    assert svcs["postgres"]["deploy"]["placement"]["constraints"] == want
    assert svcs["postgres-db-backup"]["deploy"]["placement"]["constraints"] == want
    assert svcs["apiserver"]["volumes"] == ["/var/lib/skbom-render/apiserver-data:/data"]
    assert svcs["postgres"]["volumes"] == ["/var/lib/skbom-render/postgres:/var/lib/postgresql/data"]


def test_set_pass_middlewares_and_no_tls_options(set_out):
    lab = labels(compose(set_out)["services"]["frontend"])
    assert lab["traefik.http.routers.skbom.middlewares"] == "crowdsec-bouncer@file,default-no-error-pages@file"
    assert not any(k.endswith(".tls.options") for k in lab)


# --- refusals ----------------------------------------------------------------

@pytest.mark.parametrize("vault", [
    {k: v for k, v in BASE.items() if k != "DB_PASSWORD"},
    BASE | {"DB_PASSWORD": "x" * 10},  # shorter than the 16-character minimum
], ids=["missing", "short"])
def test_missing_or_short_db_password_fails(tmp_path, vault):
    log = fails(tmp_path, vault)
    assert "needs DB_PASSWORD" in log


def test_missing_vault_block_fails(tmp_path):
    assert "no skbom vault block" in fails(tmp_path, None)


@pytest.mark.parametrize("knob", ["APISERVER_IMAGE", "FRONTEND_IMAGE", "POSTGRES_IMAGE", "POSTGRES_BACKUP_IMAGE"])
def test_unpinned_image_fails(tmp_path, knob):
    assert "must each be pinned by digest" in fails(tmp_path, BASE | {knob: "example/image:latest"})


@pytest.mark.parametrize("vault", [
    BASE | {"OIDC_ENABLED": True},
    BASE | {"OIDC_ENABLED": True, "OIDC_ISSUER": "https://sso.example.test/"},
    BASE | {"OIDC_ENABLED": True, "OIDC_CLIENT_ID": "x"},
], ids=["neither", "issuer-only", "client-id-only"])
def test_oidc_enabled_without_both_values_fails(tmp_path, vault):
    log = fails(tmp_path, vault)
    assert "OIDC_ENABLED is true but OIDC_ISSUER and/or OIDC_CLIENT_ID is empty" in log


def test_oidc_enabled_with_both_values_does_not_fail(tmp_path):
    ok(tmp_path, BASE | {"OIDC_ENABLED": True, "OIDC_ISSUER": "https://sso.example.test/", "OIDC_CLIENT_ID": "x"})


@pytest.mark.parametrize("knob", ["APISERVER_DATA_PATH", "POSTGRES_PATH", "DUMP_PATH"])
def test_local_data_path_without_data_node_fails(tmp_path, knob):
    log = fails(tmp_path, BASE | {knob: "/srv/skbom/data"})
    assert "DATA_NODE is empty" in log


def test_local_data_path_with_data_node_does_not_fail(tmp_path):
    ok(tmp_path, BASE | {"POSTGRES_PATH": "/srv/skbom/pg", "DATA_NODE": "node-1"})


# --- static ------------------------------------------------------------------

def test_playbook_default_images_equal_the_compose_literals():
    template = (ROLE / "src/config/skbom/skbom.yml.j2").read_text()
    for env in ("dev", "staging", "prod"):
        pb = (ROLE / f"deploy_skbom-{env}.yml").read_text()
        assert re.search(r'skb_default_apiserver_image: "([^"]+)"', pb).group(1) == APISERVER_IMAGE, env
        assert re.search(r'skb_default_frontend_image: "([^"]+)"', pb).group(1) == FRONTEND_IMAGE, env
        assert re.search(r'skb_default_pg_image: "([^"]+)"', pb).group(1) == PG_IMAGE, env
        assert re.search(r'skb_default_pg_backup_image: "([^"]+)"', pb).group(1) == PG_BACKUP_IMAGE, env
    assert template.count(f"default('{APISERVER_IMAGE}', true)") == 1
    assert template.count(f"default('{FRONTEND_IMAGE}', true)") == 1
    assert template.count(f"default('{PG_IMAGE}', true)") == 1
    assert template.count(f"default('{PG_BACKUP_IMAGE}', true)") == 1


def test_postgres_image_default_is_the_identical_literal_skflow_uses():
    """The whole point of reusing skflow's exact pin: the two images share a
    registry layer on any node running both roles. If this ever drifts, that
    sharing silently stops."""
    skflow_tpl = (rp.ANSIBLE / "optional/skflow/src/config/skflow/skflow.yml.j2").read_text()
    assert f"default('{PG_IMAGE}', true)" in skflow_tpl


def test_env_playbooks_differ_only_in_env_and_subnets():
    def norm(text):
        text = re.sub(r"(?<![A-Za-z])(dev|staging|prod)(?![A-Za-z])", "ENV", text)
        return re.sub(r"\d+\.\d+\.\d+\.\d+/\d+", "CIDR", text)
    prod = norm((ROLE / "deploy_skbom-prod.yml").read_text())
    for env in ("dev", "staging"):
        assert norm((ROLE / f"deploy_skbom-{env}.yml").read_text()) == prod, env
