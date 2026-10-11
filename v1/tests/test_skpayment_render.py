"""skpayment's deploy rendered through REAL Ansible (render/render_playbooks.py)
with a given vault, and the controller-side code fetch it renders executed
against a local git repo: digest-pinned image, the app code pinned by ref
AND sha256, the router built from the framework's host expressions, the
crowdsec middleware knob, the data path knob, and secrets kept out of the
compose file."""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

BASE = yaml.safe_load((rp.VARS / "skpayment.example.yml").read_text())["skpayment"]
SET = yaml.safe_load((rp.VARS / "skpayment.set.yml").read_text())["skpayment"]
DEFAULT_IMAGE = ("ghcr.io/open-webui/open-webui:main@sha256:"
                 "74093dadc9c6aabc23987a74fd8c2fb8d995b1a5b22e83b0036fb9d6af590e8c")


def render(tmp: Path, vault: dict, env: str = "prod") -> tuple[dict, Path]:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    (vars_dir / "skpayment.example.yml").write_text(yaml.safe_dump({"skpayment": vault}))
    run_dir = work / f"skpayment-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "skpayment" / f"deploy_skpayment-{env}.yml"
        res = rp.run_one(work, "optional", "skpayment", env, pb, "base")
    finally:
        rp.VARS = saved
    res["log"] = (run_dir / "ansible.log").read_text() if (run_dir / "ansible.log").exists() else ""
    return res, run_dir / "out"


def ok(tmp, vault, env="prod"):
    res, out = render(tmp, vault, env)
    assert res["status"] == "PASS", (res["findings"], res["log"][-3000:])
    return out


def compose(out, env="prod"):
    return yaml.safe_load((out / f"var/data/config/skpayment-{env}/skpayment.yml").read_text())


def labels(doc):
    return dict(l.split("=", 1) for l in doc["services"]["skpayment"]["deploy"]["labels"])


def env_file(out, env="prod"):
    return dict(l.split("=", 1) for l in (out / f"var/data/config/skpayment-{env}/skpayment.env").read_text()
                .splitlines() if l and not l.startswith("#"))


def fetch_script(out):
    bodies = [p for p in (out / "_shell").glob("*.sh") if "git clone" in p.read_text()]
    assert len(bodies) == 1
    return bodies[0]


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skp-base"), BASE)


@pytest.fixture(scope="module")
def set_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skp-set"), BASE | SET)


def test_default_service_reproduces_the_hand_deployed_gateway(base_out):
    svc = compose(base_out)["services"]["skpayment"]
    assert svc["image"] == DEFAULT_IMAGE
    assert svc["entrypoint"] == [] and svc["working_dir"] == "/"
    assert svc["command"] == ["uvicorn", "skpayment:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
    assert svc["volumes"] == ["/var/data/skpayment-prod/app/skpayment.py:/skpayment.py:ro",
                              "/var/data/config/skpayment-prod/apps.json:/config/apps.json:ro",
                              "/var/data/skpayment-prod/data:/data"]
    assert svc["deploy"]["replicas"] == 1
    assert svc["deploy"]["placement"]["constraints"] == ["node.role == worker"]
    assert svc["deploy"]["restart_policy"]["condition"] == "any"
    assert "8000/health" in " ".join(svc["healthcheck"]["test"])
    assert svc["networks"] == ["skpayment-prod", "cloud-public-prod"]


def test_default_router_uses_the_framework_host_and_no_crowdsec_chain(base_out):
    lab = labels(compose(base_out))
    assert lab == {
        "com.skt.app": "skpayment", "com.skt.env": "prod",
        "traefik.enable": "true",
        "traefik.swarm.network": "cloud-public-prod",
        "traefik.http.routers.skpayment.rule": "Host(`skpayment.render.example.test`)",
        "traefik.http.routers.skpayment.entrypoints": "websecure",
        "traefik.http.routers.skpayment.tls": "true",
        "traefik.http.routers.skpayment.tls.certresolver": "main",
        "traefik.http.routers.skpayment.tls.options": "intermediate@file",
        "traefik.http.routers.skpayment.middlewares": "default-no-crowdsec@file",
        "traefik.http.routers.skpayment.service": "skpayment",
        "traefik.http.services.skpayment.loadbalancer.server.port": "8000",
        "traefik.http.services.skpayment.loadbalancer.healthcheck.path": "/health",
        "traefik.http.services.skpayment.loadbalancer.healthcheck.interval": "30s",
        "traefik.http.services.skpayment.loadbalancer.healthcheck.timeout": "10s",
    }


def test_secrets_live_in_root_only_files_not_in_the_compose_file(base_out):
    text = (base_out / "var/data/config/skpayment-prod/skpayment.yml").read_text()
    assert "sk_test_example" not in text and "whsec_example" not in text
    assert env_file(base_out) == {
        "STRIPE_SECRET_KEY": "sk_test_example_not_a_secret",
        "STRIPE_WEBHOOK_SECRET": "whsec_example_not_a_secret",
        "BASE_URL": "https://skpayment.render.example.test",
        "DB_PATH": "/data/skpayment.db",
        "SKPAYMENT_APPS_FILE": "/config/apps.json",
    }
    assert json.loads((base_out / "var/data/config/skpayment-prod/apps.json").read_text()) == {}
    pb = (rp.ANSIBLE / "optional/skpayment/deploy_skpayment-prod.yml").read_text()
    for name in ("secrets env (root only)", "apps config (carries"):
        block = pb.split(name)[1].split("- name:")[0]
        assert "mode: '0600'" in block and "no_log: true" in block, name


@pytest.mark.parametrize("env,host,router", [("dev", "skpayment-dev.render.example.test", "skpayment-dev"),
                                             ("staging", "skpayment-staging.render.example.test", "skpayment-staging")])
def test_non_prod_hosts_and_routers(tmp_path, env, host, router):
    lab = labels(compose(ok(tmp_path, BASE, env), env))
    assert lab[f"traefik.http.routers.{router}.rule"] == f"Host(`{host}`)"
    assert lab[f"traefik.http.services.{router}.loadbalancer.server.port"] == "8000"


def test_every_knob_set(set_out):
    doc = compose(set_out)
    svc = doc["services"]["skpayment"]
    assert svc["image"] == SET["IMAGE"]
    assert svc["volumes"][2] == "/var/lib/skpayment-render/data:/data"
    assert svc["deploy"]["placement"]["constraints"] == ["node.role == worker", "node.hostname == render-node-1"]
    lab = labels(doc)
    assert lab["traefik.http.routers.skpayment.rule"] == "Host(`pay.example.test`)"
    assert lab["traefik.http.routers.skpayment.middlewares"] == "default@file"
    assert lab["traefik.http.routers.skpayment.tls.options"] == "modern@file"
    assert env_file(set_out)["BASE_URL"] == "https://pay.example.test"
    assert json.loads((set_out / "var/data/config/skpayment-prod/apps.json").read_text()) == SET["APPS"]


def test_cloudflared_base_domain_without_hostname(tmp_path):
    lab = labels(compose(ok(tmp_path, BASE | {"CLOUDFLARED": True})))
    assert lab["traefik.http.routers.skpayment.rule"] == "Host(`skpayment.example.test`)"


@pytest.mark.parametrize("over,needle", [
    ({"IMAGE": "ghcr.io/open-webui/open-webui:main"}, "pinned by digest"),
    ({"STRIPE_SECRET_KEY": ""}, "STRIPE_SECRET_KEY and"),
    ({"APP_SHA256": "abc"}, "APP_SHA256 (64 hex"),
    ({"APP_REF": "main; id"}, "APP_REF (a tag"),
    ({"DATA_PATH": "/var/lib/skpayment/data"}, "DATA_NODE is empty"),
])
def test_invalid_vaults_stop_the_play(tmp_path, over, needle):
    res, _ = render(tmp_path, BASE | over)
    assert res["status"] == "FAIL"
    assert needle in res["log"], res["log"][-2000:]


# --- the controller-side fetch, executed for real against a local repo --------

def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@example.test", "PATH": "/usr/bin:/bin", "HOME": str(cwd)})


@pytest.fixture
def app_repo(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "skpayment.py").write_text("print('v1')\n")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "v1", cwd=repo)
    git("tag", "v1.0.0", cwd=repo)
    sha1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "skpayment.py").write_text("print('v2')\n")
    git("commit", "-q", "-am", "v2", cwd=repo)
    return repo, sha1, hashlib.sha256(b"print('v1')\n").hexdigest()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.parametrize("ref_kind", ["sha", "tag"])
def test_fetch_checks_out_the_pinned_ref_and_verifies_sha256(tmp_path, app_repo, ref_kind):
    repo, sha1, digest = app_repo
    ref = sha1 if ref_kind == "sha" else "v1.0.0"
    out = ok(tmp_path, BASE | {"APP_REPO": str(repo), "APP_REF": ref, "APP_SHA256": digest})
    proc = subprocess.run(["bash", str(fetch_script(out))], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    checkout = Path(proc.stdout.strip())
    try:
        assert (checkout / "skpayment.py").read_text() == "print('v1')\n"   # not the branch head (v2)
    finally:
        shutil.rmtree(checkout.parent)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_fetch_fails_closed_on_a_sha256_mismatch(tmp_path, app_repo):
    repo, sha1, _ = app_repo
    wrong = hashlib.sha256(b"print('v2')\n").hexdigest()
    out = ok(tmp_path, BASE | {"APP_REPO": str(repo), "APP_REF": sha1, "APP_SHA256": wrong})
    proc = subprocess.run(["bash", str(fetch_script(out))], capture_output=True, text=True)
    assert proc.returncode == 1 and f"want {wrong}" in proc.stderr and proc.stdout == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_fetch_fails_on_an_unknown_ref(tmp_path, app_repo):
    repo, _, digest = app_repo
    out = ok(tmp_path, BASE | {"APP_REPO": str(repo), "APP_REF": "v9.9.9", "APP_SHA256": digest})
    proc = subprocess.run(["bash", str(fetch_script(out))], capture_output=True, text=True)
    assert proc.returncode != 0 and proc.stdout == ""


def test_compose_default_image_equals_the_playbook_pin():
    """The image default is written out in the compose template (so
    list_images.py and the image canary see the pin) and in the playbook
    (for the digest assert); they must never drift apart."""
    tpl = (rp.ANSIBLE / "optional/skpayment/src/config/skpayment/skpayment.yml.j2").read_text()
    assert f"default('{DEFAULT_IMAGE}', true)" in tpl
    for env in ("dev", "staging", "prod"):
        pb = (rp.ANSIBLE / f"optional/skpayment/deploy_skpayment-{env}.yml").read_text()
        assert f'skp_default_image: "{DEFAULT_IMAGE}"' in pb, env
