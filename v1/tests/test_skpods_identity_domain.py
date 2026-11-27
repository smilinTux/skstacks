"""skpods.IDENTITY_DOMAIN: WebIDs at https://<user>.<IDENTITY_DOMAIN>/profile/card#me.

Rendered through REAL Ansible (render/render_playbooks.py). When the knob is
set, the engine env carries SKPODS_IDENTITY_DOMAIN (read by the image's
20-skpods-identity-domain.sh start hook) and the Nextcloud service gets a
second Traefik router for one-label subdomains of the identity domain, below
the main pods router in priority. When it is unset, neither appears and the
render is unchanged. A value the image would refuse fails the play.
"""
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "skpods"
BASE = yaml.safe_load((rp.VARS / "skpods.example.yml").read_text())["skpods"]
IDENTITY = {"IDENTITY_DOMAIN": "id.example.test", "HOSTNAME": "pods.id.example.test"}
ENVS = ("dev", "staging", "prod")


def render(tmp: Path, vault: dict, env: str = "prod") -> tuple[dict, Path]:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    (vars_dir / "skpods.example.yml").write_text(yaml.safe_dump({"skpods": vault}))
    for companion in rp.VARS.glob("skpods.with.*.yml"):
        shutil.copy(companion, vars_dir / companion.name)
    run_dir = work / f"skpods-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "skpods" / f"deploy_skpods-{env}.yml"
        res = rp.run_one(work, "optional", "skpods", env, pb, "base")
    finally:
        rp.VARS = saved
    log = run_dir / "ansible.log"
    res["log"] = log.read_text() if log.exists() else ""
    return res, run_dir / "out"


def ok(tmp, vault, env="prod"):
    res, out = render(tmp, vault, env)
    assert res["status"] == "PASS", (res["findings"], res["log"][-3000:])
    return out


def fails(tmp, vault, env="prod"):
    res, _ = render(tmp, vault, env)
    assert res["status"] == "FAIL", "the deploy rendered although it must refuse this vault"
    return res["log"]


def env_file(out, env="prod"):
    return (out / f"var/data/config/skpods-{env}/nextcloud.env").read_text()


def compose_text(out, env="prod"):
    return (out / f"var/data/config/skpods-{env}/skpods.yml").read_text()


def labels(out, env="prod"):
    """Nextcloud labels as docker stack deploy sees them ($$ is compose's escape for $)."""
    c = yaml.safe_load(compose_text(out, env))
    return {k: v.replace("$$", "$") for k, v in
            (lab.split("=", 1) for lab in c["services"]["nextcloud"]["deploy"]["labels"])}


def router(env):
    return "skpods" if env == "prod" else f"skpods-{env}"


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("base"), BASE)


@pytest.fixture(scope="module")
def identity_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("identity"), BASE | IDENTITY)


def test_unset_has_no_env_var_and_no_identity_router(base_out):
    assert "SKPODS_IDENTITY_DOMAIN" not in env_file(base_out)
    assert "identity" not in compose_text(base_out)
    assert not [v for v in labels(base_out).values() if "HostRegexp" in v]


def test_set_passes_the_env_var(identity_out):
    lines = env_file(identity_out).splitlines()
    assert "SKPODS_IDENTITY_DOMAIN=id.example.test" in lines
    assert "OVERWRITEHOST=pods.id.example.test" in lines


def test_set_adds_a_lower_priority_wildcard_router_on_the_same_service(identity_out):
    lab = labels(identity_out)
    main, ident = "traefik.http.routers.skpods", "traefik.http.routers.skpods-identity"
    assert lab[f"{main}.rule"] == "Host(`pods.id.example.test`)"
    assert lab[f"{ident}.rule"] == r"HostRegexp(`^[a-z0-9-]+\.id\.example\.test$`)"
    for key in ("entrypoints", "tls", "middlewares", "service"):
        assert lab[f"{ident}.{key}"] == lab[f"{main}.{key}"], key
    # Traefik: the higher priority wins, so pods.<domain> (which the regexp
    # also matches) stays on the main router, and the router stays above the
    # edge's per-domain catch-alls (priority 1).
    assert 1 < int(lab[f"{ident}.priority"]) < int(lab[f"{main}.priority"])
    # No ACME resolver by default: TLS comes from the certificate Traefik
    # already holds (for example an origin certificate for *.<domain>).
    assert f"{ident}.tls.certresolver" not in lab
    assert not [k for k in lab if k.startswith(f"{ident}.tls.domains")]


def test_wildcard_regexp_matches_only_one_label_user_hosts(identity_out):
    rule = labels(identity_out)["traefik.http.routers.skpods-identity.rule"]
    rx = re.compile(re.fullmatch(r"HostRegexp\(`(.+)`\)", rule).group(1))
    assert rx.match("alice.id.example.test")
    assert rx.match("a-b-9.id.example.test")
    assert rx.match("pods.id.example.test"), "covered, but the main router outranks it"
    assert not rx.match("id.example.test")
    assert not rx.match("a.b.id.example.test")
    assert not rx.match("alice.idxexample.test")
    assert not rx.match("alice.id.example.test.evil.test")


def test_identity_certresolver_requests_the_wildcard(tmp_path):
    out = ok(tmp_path, BASE | IDENTITY | {"IDENTITY_CERTRESOLVER": "dns", "TLS_OPTIONS": "modern@file"})
    lab = labels(out)
    ident = "traefik.http.routers.skpods-identity"
    assert lab[f"{ident}.tls.certresolver"] == "dns"
    assert lab[f"{ident}.tls.domains[0].main"] == "id.example.test"
    assert lab[f"{ident}.tls.domains[0].sans"] == "*.id.example.test"
    assert lab[f"{ident}.tls.options"] == "modern@file"
    assert lab["traefik.http.routers.skpods.tls.certresolver"] == "main"


@pytest.mark.parametrize("env", ["dev", "staging"])
def test_dev_and_staging_router_names(tmp_path, env):
    lab = labels(ok(tmp_path, BASE | IDENTITY, env), env)
    assert lab[f"traefik.http.routers.{router(env)}-identity.service"] == router(env)


@pytest.mark.parametrize("value", [
    "ID.example.test", "id", "https://id.example.test", "id.example.test:443",
    "id.example.test/", "*.id.example.test", ".id.example.test", "id.example.test.",
    "id..example.test", "-id.example.test", "id-.example.test", "id.example.test\n",
    "id example.test", "a" * 64 + ".example.test",
])
def test_bad_identity_domain_fails(tmp_path, value):
    log = fails(tmp_path, BASE | {"IDENTITY_DOMAIN": value, "HOSTNAME": f"pods.{value}"})
    assert "IDENTITY_DOMAIN must be a plain lowercase hostname" in log


def test_hostname_must_be_the_pods_host_of_the_identity_domain(tmp_path):
    fails(tmp_path, BASE | {"IDENTITY_DOMAIN": "id.example.test"})


def test_bad_identity_certresolver_fails(tmp_path):
    fails(tmp_path, BASE | IDENTITY | {"IDENTITY_CERTRESOLVER": "dns\nx"})


def test_env_playbooks_differ_only_in_env_and_subnets():
    def norm(text):
        text = re.sub(r"(?<![A-Za-z])(dev|staging|prod)(?![A-Za-z])", "ENV", text)
        return re.sub(r"\d+\.\d+\.\d+\.\d+/\d+", "CIDR", text)
    prod = norm((ROLE / "deploy_skpods-prod.yml").read_text())
    for env in ENVS:
        assert norm((ROLE / f"deploy_skpods-{env}.yml").read_text()) == prod, env
