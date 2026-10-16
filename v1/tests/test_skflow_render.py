"""skflow's deploy rendered through REAL Ansible (render/render_playbooks.py)
with a given vault: the digest-pinned Windmill and Postgres images, the
service set with the stack's own database and with an external DATABASE_URL,
the URL-encoded database password, the router set without and with
SSO_MIDDLEWARE (the API router never gets forward auth, the outpost route goes
to sksso), the data path / data node guard, and loud failures for a missing
or short password, an unpinned image and a bad SSO_EXEMPT_PREFIXES."""
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "skflow"
BASE = yaml.safe_load((rp.VARS / "skflow.example.yml").read_text())["skflow"]
SET = yaml.safe_load((rp.VARS / "skflow.set.yml").read_text())["skflow"]
IMAGE = ("ghcr.io/windmill-labs/windmill:1.837.0@sha256:"
         "661aa3e338be081812ef301a2be8defc50bd9d3c4dde89f8cd53a763e6452a74")
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
    doc = {} if vault is None else {"skflow": vault}
    (vars_dir / "skflow.example.yml").write_text(yaml.safe_dump(doc))
    run_dir = work / f"skflow-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "skflow" / f"deploy_skflow-{env}.yml"
        res = rp.run_one(work, "optional", "skflow", env, pb, "base")
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
    return yaml.safe_load((out / f"var/data/config/skflow-{env}/skflow.yml").read_text())


def envfile(out, name="skflow.env", env="prod"):
    lines = (out / f"var/data/config/skflow-{env}/{name}").read_text().splitlines()
    return dict(l.split("=", 1) for l in lines if l and not l.startswith("#"))


def labels(svc):
    return dict(l.split("=", 1) for l in svc["deploy"]["labels"])


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skflow-base"), BASE)


@pytest.fixture(scope="module")
def set_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skflow-set"), BASE | SET)


# --- happy path: defaults --------------------------------------------------

def test_default_services_images_and_modes(base_out):
    svcs = compose(base_out)["services"]
    assert sorted(svcs) == ["postgres", "postgres-db-backup", "server", "worker"]
    assert svcs["server"]["image"] == IMAGE and svcs["worker"]["image"] == IMAGE
    assert svcs["postgres"]["image"] == PG_IMAGE and svcs["postgres-db-backup"]["image"] == PG_BACKUP_IMAGE
    assert svcs["server"]["environment"] == ["MODE=server"]
    assert svcs["worker"]["environment"] == ["MODE=worker", "WORKER_GROUP=default"]
    assert svcs["worker"]["deploy"]["replicas"] == 2
    assert svcs["postgres"]["deploy"]["replicas"] == 1
    assert svcs["postgres"]["deploy"]["update_config"]["order"] == "stop-first"
    for svc in svcs.values():
        assert svc["deploy"]["restart_policy"]["condition"] == "any"
        assert svc["deploy"]["placement"]["constraints"] == ["node.role == worker"]
    assert svcs["server"]["networks"] == ["skflow-prod", "cloud-public-prod"]
    assert svcs["worker"]["networks"] == ["skflow-prod"]
    assert "8000/api/version" in svcs["server"]["healthcheck"]["test"][1]
    assert svcs["postgres"]["volumes"] == ["/var/data/runtime/skflow-prod/postgres:/var/lib/postgresql/data"]
    assert svcs["postgres-db-backup"]["volumes"] == ["/var/data/skflow-prod/database-dump:/dump"]
    assert "pg_dumpall" in svcs["postgres-db-backup"]["entrypoint"][2]


def test_default_env_points_at_the_stack_postgres(base_out):
    e = envfile(base_out)
    assert e["DATABASE_URL"] == (
        f"postgres://windmill:{BASE['DB_PASSWORD']}@postgres:5432/windmill?sslmode=disable")
    assert e["BASE_URL"] == "https://skflow.render.example.test"
    assert e["DISABLE_NSJAIL"] == "true"
    assert "SUPERADMIN_SECRET" not in e and "COOKIE_DOMAIN" not in e
    assert envfile(base_out, "postgres.env")["POSTGRES_PASSWORD"] == BASE["DB_PASSWORD"]
    assert envfile(base_out, "postgres-backup.env")["PGPASSWORD"] == BASE["DB_PASSWORD"]


def test_default_single_router_no_sso(base_out):
    lab = labels(compose(base_out)["services"]["server"])
    assert lab["traefik.http.routers.skflow.rule"] == "Host(`skflow.render.example.test`)"
    assert lab["traefik.http.routers.skflow.middlewares"] == "default-no-crowdsec@file"
    assert lab["traefik.http.routers.skflow.tls.options"] == "intermediate@file"
    assert lab["traefik.http.services.skflow.loadbalancer.server.port"] == "8000"
    assert not any(k.startswith(("traefik.http.routers.skflow-api", "traefik.http.routers.skflow-outpost")) for k in lab)


def test_env_files_are_root_only_and_never_logged():
    pb = (ROLE / "deploy_skflow-prod.yml").read_text()
    block = pb.split("Process {{ app }} environment templates")[1].split("- name:")[0]
    assert "mode: '0600'" in block and "no_log: true" in block


def test_password_with_url_special_characters_is_encoded(tmp_path):
    special = "p@ss/w:rd?with#chars"  # URL-special characters, not a credential
    out = ok(tmp_path, BASE | {"DB_PASSWORD": special})
    encoded = "p%40ss%2Fw%3Ard%3Fwith%23chars"
    assert envfile(out)["DATABASE_URL"].startswith("postgres://windmill:" + encoded + "@postgres:5432/")
    assert envfile(out, "postgres.env")["POSTGRES_PASSWORD"] == special


@pytest.mark.parametrize("env,host,router,subnet", [
    ("dev", "skflow-dev.render.example.test", "skflow-dev", "172.16.248.0/24"),
    ("staging", "skflow-staging.render.example.test", "skflow-staging", "172.16.247.0/24"),
])
def test_dev_and_staging_hosts_routers_and_subnets(tmp_path, env, host, router, subnet):
    out = ok(tmp_path, BASE, env)
    lab = labels(compose(out, env)["services"]["server"])
    assert lab[f"traefik.http.routers.{router}.rule"] == f"Host(`{host}`)"
    assert subnet in (ROLE / f"deploy_skflow-{env}.yml").read_text()


# --- every knob set: external db, SSO --------------------------------------

def test_set_pass_external_database_drops_postgres_and_backup(set_out):
    svcs = compose(set_out)["services"]
    assert sorted(svcs) == ["server", "worker", "worker-native"]
    assert envfile(set_out)["DATABASE_URL"] == SET["DATABASE_URL"]
    assert svcs["server"]["image"] == SET["WINDMILL_IMAGE"]


def test_set_pass_workers_and_settings(set_out):
    svcs = compose(set_out)["services"]
    assert svcs["worker"]["deploy"]["replicas"] == 3
    assert svcs["worker"]["environment"] == ["MODE=worker", "WORKER_GROUP=docs"]
    assert svcs["worker-native"]["environment"] == ["MODE=worker", "WORKER_GROUP=native", "NATIVE_MODE=true"]
    assert svcs["server"]["deploy"]["replicas"] == 2
    assert svcs["server"]["deploy"]["placement"]["constraints"] == SET["PLACEMENT_CONSTRAINTS"]
    e = envfile(set_out)
    assert e["SUPERADMIN_SECRET"] == SET["SUPERADMIN_SECRET"]
    assert e["COOKIE_DOMAIN"] == "example.test" and e["DISABLE_NSJAIL"] == "false"
    assert e["ZOMBIE_JOB_TIMEOUT"] == "60" and e["RUST_LOG"] == "debug" and e["JSON_FMT"] == "true"


def test_set_pass_sso_routers(set_out):
    lab = labels(compose(set_out)["services"]["server"])
    assert lab["traefik.http.routers.skflow.middlewares"] == "crowdsec-bouncer@file,default@file,authentik@file"
    assert lab["traefik.http.routers.skflow-api.rule"] == (
        "Host(`skflow.example.test`) && (PathPrefix(`/api/`) || PathPrefix(`/ws/`))")
    assert "authentik" not in lab["traefik.http.routers.skflow-api.middlewares"]
    assert lab["traefik.http.routers.skflow-outpost.rule"] == (
        "Host(`skflow.example.test`) && PathPrefix(`/outpost.goauthentik.io/`)")
    assert lab["traefik.http.routers.skflow-outpost.service"] == "sksso-alt-server@swarm"
    assert not any(k.endswith(".tls.options") for k in lab)


@pytest.mark.parametrize("env,service", [("prod", "sksso-server@swarm"), ("dev", "sksso-dev-server@swarm")])
def test_sso_outpost_defaults_to_the_sksso_server_of_the_same_env(tmp_path, env, service):
    out = ok(tmp_path, BASE | {"SSO_MIDDLEWARE": "authentik@file"}, env)
    sfx = "" if env == "prod" else f"-{env}"
    lab = labels(compose(out, env)["services"]["server"])
    assert lab[f"traefik.http.routers.skflow{sfx}-outpost.service"] == service
    assert lab[f"traefik.http.routers.skflow{sfx}-api.rule"].endswith("(PathPrefix(`/api/`))")


def test_empty_exempt_list_puts_everything_behind_sso(tmp_path):
    out = ok(tmp_path, BASE | {"SSO_MIDDLEWARE": "authentik@file", "SSO_EXEMPT_PREFIXES": []})
    lab = labels(compose(out)["services"]["server"])
    assert not any(k.startswith("traefik.http.routers.skflow-api") for k in lab)
    assert "traefik.http.routers.skflow-outpost.rule" in lab


def test_data_node_pins_only_the_database_services(tmp_path):
    out = ok(tmp_path, BASE | {"DATA_NODE": "node-1", "POSTGRES_PATH": "/srv/skflow/pg", "DUMP_PATH": "/srv/skflow/dump"})
    svcs = compose(out)["services"]
    pin = "node.hostname == node-1"
    assert pin in svcs["postgres"]["deploy"]["placement"]["constraints"]
    assert pin in svcs["postgres-db-backup"]["deploy"]["placement"]["constraints"]
    assert pin not in svcs["server"]["deploy"]["placement"]["constraints"]
    assert svcs["postgres"]["volumes"] == ["/srv/skflow/pg:/var/lib/postgresql/data"]


# --- refusals ----------------------------------------------------------------

@pytest.mark.parametrize("vault", [
    {k: v for k, v in BASE.items() if k != "DB_PASSWORD"},
    BASE | {"DB_PASSWORD": "x" * 10},  # shorter than the 16-character minimum
])
def test_missing_or_short_db_password_fails(tmp_path, vault):
    log = fails(tmp_path, vault)
    assert "needs DB_PASSWORD" in log
    assert "s3cr3tVal9" not in log  # the refusal never prints the value


def test_external_database_url_needs_no_password(tmp_path):
    vault = {k: v for k, v in BASE.items() if k != "DB_PASSWORD"} | {"DATABASE_URL": "postgres://u@db.example.test/w"}
    ok(tmp_path, vault)


def test_short_superadmin_secret_fails(tmp_path):
    assert "SUPERADMIN_SECRET" in fails(tmp_path, BASE | {"SUPERADMIN_SECRET": "too-short"})


def test_missing_vault_block_fails(tmp_path):
    assert "no skflow vault block" in fails(tmp_path, None)


@pytest.mark.parametrize("knob", ["WINDMILL_IMAGE", "POSTGRES_IMAGE", "POSTGRES_BACKUP_IMAGE"])
def test_unpinned_image_fails(tmp_path, knob):
    assert "must be pinned by digest" in fails(tmp_path, BASE | {knob: "example/image:latest"})


@pytest.mark.parametrize("value", ["/api/", ["api/"], ["/api/ with space"]])
def test_bad_exempt_prefixes_fail(tmp_path, value):
    fails(tmp_path, BASE | {"SSO_MIDDLEWARE": "authentik@file", "SSO_EXEMPT_PREFIXES": value})


def test_negative_worker_replicas_fail(tmp_path):
    fails(tmp_path, BASE | {"WORKER_REPLICAS": -1})


def test_local_postgres_path_without_data_node_fails(tmp_path):
    assert "DATA_NODE is empty" in fails(tmp_path, BASE | {"POSTGRES_PATH": "/srv/skflow/pg"})


# --- static ------------------------------------------------------------------

def test_playbook_default_images_equal_the_compose_literals():
    template = (ROLE / "src/config/skflow/skflow.yml.j2").read_text()
    for env in ("dev", "staging", "prod"):
        pb = (ROLE / f"deploy_skflow-{env}.yml").read_text()
        assert re.search(r'sf_default_image: "([^"]+)"', pb).group(1) == IMAGE, env
        assert re.search(r'sf_default_pg_image: "([^"]+)"', pb).group(1) == PG_IMAGE, env
        assert re.search(r'sf_default_pg_backup_image: "([^"]+)"', pb).group(1) == PG_BACKUP_IMAGE, env
    assert template.count(f"default('{IMAGE}', true)") == 3
    assert template.count(f"default('{PG_IMAGE}', true)") == 1
    assert template.count(f"default('{PG_BACKUP_IMAGE}', true)") == 1


def test_env_playbooks_differ_only_in_env_and_subnets():
    def norm(text):
        text = re.sub(r"(?<![A-Za-z])(dev|staging|prod)(?![A-Za-z])", "ENV", text)
        return re.sub(r"\d+\.\d+\.\d+\.\d+/\d+", "CIDR", text)
    prod = norm((ROLE / "deploy_skflow-prod.yml").read_text())
    for env in ("dev", "staging"):
        assert norm((ROLE / f"deploy_skflow-{env}.yml").read_text()) == prod, env
