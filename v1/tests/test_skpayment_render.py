"""skpayment's deploy rendered through REAL Ansible (render/render_playbooks.py)
with a given vault, and the controller-side code fetch it renders executed
against a local git repo: digest-pinned image, the app code pinned by ref
AND sha256, the router built from the framework's host expressions, the
crowdsec middleware knobs (site router with CrowdSec, the Stripe webhook
router exempt and higher priority), the data path knob, and secrets kept out of the
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


def test_default_routers_use_the_framework_host_crowdsec_on_the_site_webhook_exempt(base_out):
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
        "traefik.http.routers.skpayment.middlewares": "default@file",
        "traefik.http.routers.skpayment.service": "skpayment",
        "traefik.http.routers.skpayment-webhook.rule":
            "Host(`skpayment.render.example.test`) && Path(`/webhook/stripe`)",
        "traefik.http.routers.skpayment-webhook.priority": "1000",
        "traefik.http.routers.skpayment-webhook.entrypoints": "websecure",
        "traefik.http.routers.skpayment-webhook.tls": "true",
        "traefik.http.routers.skpayment-webhook.tls.certresolver": "main",
        "traefik.http.routers.skpayment-webhook.tls.options": "intermediate@file",
        "traefik.http.routers.skpayment-webhook.middlewares": "default-no-crowdsec@file",
        "traefik.http.routers.skpayment-webhook.service": "skpayment",
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
    ({"STRIPE_WEBHOOK_SECRET": ""}, "both or neither"),
    ({"STRIPE_SECRET_KEY": ""}, "both or neither"),
    ({"STRIPE_SECRET_KEY": "", "STRIPE_WEBHOOK_SECRET": "", "APP_REF": "17b00804c204f9cd371938c701bd63039b0d1c8b",
      "APP_SHA256": "c957335b6f910b81870b1dc229a294be8a40f1cc935688801193e00a98f83ff8"}, "accepts unsigned webhooks"),
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


def test_tls_options_none_omits_the_label(tmp_path):
    """TLS_OPTIONS: none leaves the router on Traefik's default TLS options
    (a "default@file" reference is rejected by Traefik as unknown)."""
    lab = labels(compose(ok(tmp_path, BASE | {"TLS_OPTIONS": "none"})))
    assert not [k for k in lab if k.endswith(".tls.options")]
    assert lab["traefik.http.routers.skpayment.tls"] == "true"
    assert lab["traefik.http.routers.skpayment.tls.certresolver"] == "main"


# --- "Stripe not configured" mode (card 1c2347e9) ------------------------------------

APP_REF_DEFAULT = "d3138a22b8b24553ebf552fed7315c8d413cd399"     # smilinTux/skpayments v1.1.0
APP_SHA256_DEFAULT = "5a88f86bf6b22ba991568152cd3341d80afa67e003478787fb7eaa1186093d8e"


def test_both_stripe_secrets_empty_is_allowed_and_renders_empty_values(tmp_path):
    out = ok(tmp_path, BASE | {"STRIPE_SECRET_KEY": "", "STRIPE_WEBHOOK_SECRET": ""})
    env = env_file(out)
    assert env["STRIPE_SECRET_KEY"] == "" and env["STRIPE_WEBHOOK_SECRET"] == ""
    assert env["BASE_URL"] == "https://skpayment.render.example.test"


def test_both_stripe_secrets_absent_is_allowed(tmp_path):
    vault = {k: v for k, v in BASE.items() if not k.startswith("STRIPE_")}
    env = env_file(ok(tmp_path, vault))
    assert env["STRIPE_SECRET_KEY"] == "" and env["STRIPE_WEBHOOK_SECRET"] == ""


def test_default_app_pin_is_the_release_that_rejects_unverified_webhooks(base_out):
    """17b0080 accepted any unsigned webhook when STRIPE_WEBHOOK_SECRET was
    empty; v1.1.0 (d3138a2) answers 503 instead. The fetch script pins both
    the commit and the file hash."""
    body = fetch_script(base_out).read_text()
    assert APP_REF_DEFAULT in body and APP_SHA256_DEFAULT in body
    for env in ("dev", "staging", "prod"):
        pb = (rp.ANSIBLE / f"optional/skpayment/deploy_skpayment-{env}.yml").read_text()
        assert f'skp_default_ref: "{APP_REF_DEFAULT}"' in pb and f'skp_default_sha256: "{APP_SHA256_DEFAULT}"' in pb


def test_old_app_pin_still_deploys_with_secrets_set(tmp_path):
    """An instance that pins the old release and HAS a webhook secret keeps working."""
    ok(tmp_path, BASE | {"APP_REF": "17b00804c204f9cd371938c701bd63039b0d1c8b",
                         "APP_SHA256": "c957335b6f910b81870b1dc229a294be8a40f1cc935688801193e00a98f83ff8"})


# --- CrowdSec on the site, the Stripe webhook exempted (two routers) -----------------

STRIPE_WEBHOOK_IPS = [   # https://stripe.com/files/ips/ips_webhooks.json, fetched 2026-10-13
    "3.18.12.63", "3.69.109.8", "3.120.168.93", "3.130.192.231", "13.235.14.237", "13.235.122.149",
    "18.211.135.69", "35.154.171.200", "35.157.207.129", "52.15.183.38", "54.88.130.119",
    "54.88.130.237", "54.187.174.169", "54.187.205.235", "54.187.216.72",
]


def routers(lab):
    return sorted({k.split(".")[3] for k in lab if k.startswith("traefik.http.routers.")})


def effective_priority(lab, router):
    """Traefik's router priority: the explicit label, else the rule's length."""
    explicit = lab.get(f"traefik.http.routers.{router}.priority")
    return int(explicit) if explicit is not None else len(lab[f"traefik.http.routers.{router}.rule"])


def test_default_splits_into_a_crowdsec_site_router_and_an_exempt_webhook_router(base_out):
    lab = labels(compose(base_out))
    assert routers(lab) == ["skpayment", "skpayment-webhook"]
    assert lab["traefik.http.routers.skpayment.middlewares"] == "default@file"
    w = "traefik.http.routers.skpayment-webhook"
    assert lab[f"{w}.rule"] == "Host(`skpayment.render.example.test`) && Path(`/webhook/stripe`)"
    assert lab[f"{w}.middlewares"] == "default-no-crowdsec@file"
    assert lab[f"{w}.entrypoints"] == "websecure"
    assert lab[f"{w}.tls"] == "true"
    assert lab[f"{w}.tls.certresolver"] == "main"
    assert lab[f"{w}.tls.options"] == "intermediate@file"
    assert lab[f"{w}.service"] == "skpayment"      # the same backend, no second service
    assert not [k for k in lab if ".middlewares.skpayment" in k]   # no IP allowlist by default


def test_webhook_router_outranks_the_site_router(base_out):
    lab = labels(compose(base_out))
    assert lab["traefik.http.routers.skpayment-webhook.priority"] == "1000"
    assert "traefik.http.routers.skpayment.priority" not in lab
    assert effective_priority(lab, "skpayment-webhook") > effective_priority(lab, "skpayment")


def test_webhook_router_outranks_a_long_hostname(tmp_path):
    lab = labels(compose(ok(tmp_path, BASE | {"HOSTNAME": "pay." + "a" * 200 + ".example.test"})))
    assert effective_priority(lab, "skpayment-webhook") > effective_priority(lab, "skpayment")


def test_webhook_knobs_set(tmp_path):
    lab = labels(compose(ok(tmp_path, BASE | {
        "MIDDLEWARES": "crowdsec-bouncer@file,default-no-crowdsec@file",
        "WEBHOOK_PATH": "/stripe/webhook",
        "WEBHOOK_MIDDLEWARES": "default-no-crowdsec@file,gzip@file",
        "WEBHOOK_PRIORITY": 500,
        "TLS_OPTIONS": "none"})))
    assert lab["traefik.http.routers.skpayment.middlewares"] == "crowdsec-bouncer@file,default-no-crowdsec@file"
    w = "traefik.http.routers.skpayment-webhook"
    assert lab[f"{w}.rule"] == "Host(`skpayment.render.example.test`) && Path(`/stripe/webhook`)"
    assert lab[f"{w}.middlewares"] == "default-no-crowdsec@file,gzip@file"
    assert lab[f"{w}.priority"] == "500"
    assert not [k for k in lab if k.endswith(".tls.options")]


def test_webhook_path_none_keeps_a_single_router(tmp_path):
    lab = labels(compose(ok(tmp_path, BASE | {"WEBHOOK_PATH": "none"})))
    assert routers(lab) == ["skpayment"]
    assert lab["traefik.http.routers.skpayment.middlewares"] == "default@file"


def test_webhook_ip_allowlist_default_is_stripes_published_list(tmp_path):
    lab = labels(compose(ok(tmp_path, BASE | {"WEBHOOK_IP_ALLOWLIST": True})))
    m = "traefik.http.middlewares.skpayment-stripe-ips.ipallowlist"
    assert lab[f"{m}.sourcerange"] == ",".join(STRIPE_WEBHOOK_IPS)
    assert f"{m}.ipstrategy.depth" not in lab
    # the allowlist runs first, then the (no-CrowdSec) chain; the site router is untouched
    assert lab["traefik.http.routers.skpayment-webhook.middlewares"] == "skpayment-stripe-ips,default-no-crowdsec@file"
    assert lab["traefik.http.routers.skpayment.middlewares"] == "default@file"


def test_webhook_ip_allowlist_custom_ranges_and_depth(tmp_path):
    lab = labels(compose(ok(tmp_path, BASE | {"WEBHOOK_IP_ALLOWLIST": True,
                                              "WEBHOOK_SOURCE_RANGES": ["192.0.2.0/24", "198.51.100.7"],
                                              "WEBHOOK_IP_STRATEGY_DEPTH": 1})))
    m = "traefik.http.middlewares.skpayment-stripe-ips.ipallowlist"
    assert lab[f"{m}.sourcerange"] == "192.0.2.0/24,198.51.100.7"
    assert lab[f"{m}.ipstrategy.depth"] == "1"


@pytest.mark.parametrize("env,router", [("dev", "skpayment-dev"), ("staging", "skpayment-staging")])
def test_non_prod_webhook_router_and_allowlist_names(tmp_path, env, router):
    lab = labels(compose(ok(tmp_path, BASE | {"WEBHOOK_IP_ALLOWLIST": True}, env), env))
    assert routers(lab) == [router, f"{router}-webhook"]
    assert lab[f"traefik.http.routers.{router}-webhook.service"] == router
    assert lab[f"traefik.http.routers.{router}-webhook.middlewares"] == \
        f"{router}-stripe-ips,default-no-crowdsec@file"
    assert f"traefik.http.middlewares.{router}-stripe-ips.ipallowlist.sourcerange" in lab


@pytest.mark.parametrize("over,needle", [
    ({"WEBHOOK_PATH": "webhook/stripe"}, "WEBHOOK_PATH"),
    ({"WEBHOOK_PATH": "/webhook`) || Host(`x"}, "WEBHOOK_PATH"),
    ({"WEBHOOK_PRIORITY": "high"}, "WEBHOOK_PRIORITY"),
    ({"WEBHOOK_PRIORITY": -1}, "WEBHOOK_PRIORITY"),
    ({"WEBHOOK_MIDDLEWARES": "default-no-crowdsec@file`"}, "WEBHOOK_MIDDLEWARES"),
    ({"WEBHOOK_IP_ALLOWLIST": True, "WEBHOOK_SOURCE_RANGES": "3.18.12.63"}, "WEBHOOK_SOURCE_RANGES"),
    ({"WEBHOOK_IP_ALLOWLIST": True, "WEBHOOK_SOURCE_RANGES": ["not an ip"]}, "WEBHOOK_SOURCE_RANGES"),
    ({"WEBHOOK_IP_ALLOWLIST": True, "WEBHOOK_IP_STRATEGY_DEPTH": "x"}, "WEBHOOK_IP_STRATEGY_DEPTH"),
])
def test_invalid_webhook_knobs_stop_the_play(tmp_path, over, needle):
    res, _ = render(tmp_path, BASE | over)
    assert res["status"] == "FAIL"
    assert needle in res["log"], res["log"][-2000:]


def test_stripe_ip_default_is_identical_in_every_env_playbook():
    for env in ("dev", "staging", "prod"):
        pb = yaml.safe_load((rp.ANSIBLE / f"optional/skpayment/deploy_skpayment-{env}.yml").read_text())
        play_vars = next(p["vars"] for p in pb if "vars" in p and "skp_default_image" in p["vars"])
        assert play_vars["skp_default_stripe_webhook_ips"] == STRIPE_WEBHOOK_IPS, env
