"""skpulse (Uptime Kuma) rendered through REAL Ansible (render/render_playbooks.py).

Sign-in: AUTH_MODE sso (default, Authentik forward auth on the dashboard,
login page and /socket.io/, which is NOT a separate path: it hangs off the
same Host rule) or local (Kuma's own login only, no forward auth), with a
guard that refuses a local-mode vault that also sets SSO_MIDDLEWARE.
Push monitors, public status pages, badges and the status-page SPA assets
always render on a separate, higher-priority router that never carries
SSO_MIDDLEWARE, in either mode.
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

ROLE = rp.ANSIBLE / "optional" / "skpulse"
BASE = yaml.safe_load((rp.VARS / "skpulse.example.yml").read_text())["skpulse"] or {}


def render(tmp: Path, vault, env: str = "prod") -> tuple[dict, Path]:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    doc = {} if vault is None else {"skpulse": vault}
    (vars_dir / "skpulse.example.yml").write_text(yaml.safe_dump(doc))
    run_dir = work / f"skpulse-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "skpulse" / f"deploy_skpulse-{env}.yml"
        res = rp.run_one(work, "optional", "skpulse", env, pb, "base")
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
    return yaml.safe_load((out / f"var/data/config/skpulse-{env}/skpulse.yml").read_text())


def labels(svc):
    return dict(l.split("=", 1) for l in svc["deploy"]["labels"])


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("skpulse-base"), BASE)


# --- happy path: defaults (AUTH_MODE sso) -----------------------------------

def test_default_single_service_one_replica(base_out):
    svcs = compose(base_out)["services"]
    assert list(svcs) == ["skpulse"]
    svc = svcs["skpulse"]
    assert svc["deploy"]["replicas"] == 1
    assert svc["deploy"]["placement"]["constraints"] == ["node.role == worker"]
    assert svc["networks"] == ["cloud-public-prod", "skpulse-prod"]


def test_default_auth_mode_is_sso_authentik_on_the_main_router(base_out):
    lab = labels(compose(base_out)["services"]["skpulse"])
    assert lab["traefik.http.routers.skpulse.rule"] == "Host(`skpulse.render.example.test`)"
    assert lab["traefik.http.routers.skpulse.middlewares"] == "default-no-crowdsec@file,authentik@file"
    assert lab["traefik.http.routers.skpulse.priority"] == "5"
    assert lab["traefik.http.routers.skpulse.service"] == "skpulse"


def test_sso_route_covers_socketio_and_the_dashboard_with_no_carve_out(base_out):
    # /socket.io/ is NOT a separate path and NEVER appears in the main
    # router's rule or in the bypass router's rule: it rides the same Host
    # rule as the dashboard, so AUTH_MODE sso gates it exactly like every
    # other page.
    lab = labels(compose(base_out)["services"]["skpulse"])
    assert "socket" not in lab["traefik.http.routers.skpulse.rule"].lower()
    assert "socket" not in lab["traefik.http.routers.skpulse-public.rule"].lower()
    # the main router's rule is a bare Host rule: no PathPrefix carve-out
    assert lab["traefik.http.routers.skpulse.rule"] == "Host(`skpulse.render.example.test`)"


def test_bypass_router_present_with_push_status_badge_and_assets_never_sso(base_out):
    lab = labels(compose(base_out)["services"]["skpulse"])
    rule = lab["traefik.http.routers.skpulse-public.rule"]
    for needle in ("PathPrefix(`/api/push/`)", "PathPrefix(`/api/status-page/`)",
                   "PathPrefix(`/api/badge/`)", "PathPrefix(`/status`)",
                   "PathPrefix(`/assets`)", "PathPrefix(`/upload`)"):
        assert needle in rule, rule
    assert lab["traefik.http.routers.skpulse-public.priority"] == "100"
    assert lab["traefik.http.routers.skpulse-public.middlewares"] == "default-no-crowdsec@file"
    assert "authentik" not in lab["traefik.http.routers.skpulse-public.middlewares"]
    assert lab["traefik.http.routers.skpulse-public.service"] == "skpulse"


def test_metrics_router_absent_by_default(base_out):
    lab = labels(compose(base_out)["services"]["skpulse"])
    assert not any(k.startswith("traefik.http.routers.skpulse-metrics") for k in lab)


def test_metrics_router_opt_in_never_sso(tmp_path):
    out = ok(tmp_path, BASE | {"METRICS_BYPASS_SSO": True})
    lab = labels(compose(out)["services"]["skpulse"])
    assert lab["traefik.http.routers.skpulse-metrics.rule"] == "Host(`skpulse.render.example.test`) && Path(`/metrics`)"
    assert lab["traefik.http.routers.skpulse-metrics.priority"] == "100"
    assert "authentik" not in lab["traefik.http.routers.skpulse-metrics.middlewares"]


# --- default-unchanged: AUTH_MODE unset == AUTH_MODE sso --------------------

@pytest.mark.parametrize("mode", ["SSO", "sso", ""])
def test_auth_mode_sso_spellings_render_like_the_default(tmp_path, base_out, mode):
    out = ok(tmp_path, BASE | {"AUTH_MODE": mode})
    rel = "var/data/config/skpulse-prod/skpulse.yml"
    assert (out / rel).read_text() == (base_out / rel).read_text()


def test_auth_mode_sso_takes_a_custom_forward_auth_middleware(tmp_path):
    out = ok(tmp_path, BASE | {"AUTH_MODE": "sso", "SSO_MIDDLEWARE": "authentik-other@file"})
    lab = labels(compose(out)["services"]["skpulse"])
    assert lab["traefik.http.routers.skpulse.middlewares"] == "default-no-crowdsec@file,authentik-other@file"
    # the bypass router is unaffected
    assert "authentik" not in lab["traefik.http.routers.skpulse-public.middlewares"]


# --- AUTH_MODE local ---------------------------------------------------------

def test_auth_mode_local_is_kuma_own_login_no_sso_anywhere(tmp_path):
    out = ok(tmp_path, BASE | {"AUTH_MODE": "local"})
    lab = labels(compose(out)["services"]["skpulse"])
    assert lab["traefik.http.routers.skpulse.middlewares"] == "default-no-crowdsec@file"
    assert "authentik" not in lab["traefik.http.routers.skpulse.middlewares"]
    assert "authentik" not in lab["traefik.http.routers.skpulse-public.middlewares"]
    # the bypass router still renders in local mode too (push/status stay public either way)
    assert lab["traefik.http.routers.skpulse-public.priority"] == "100"


@pytest.mark.parametrize("mode", ["LOCAL", "Local"])
def test_auth_mode_local_spellings(tmp_path, mode):
    out = ok(tmp_path, BASE | {"AUTH_MODE": mode})
    lab = labels(compose(out)["services"]["skpulse"])
    assert lab["traefik.http.routers.skpulse.middlewares"] == "default-no-crowdsec@file"


# --- contradictory configs are refused --------------------------------------

def test_unknown_auth_mode_fails(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "none"})
    assert "skpulse.AUTH_MODE must be 'sso' or 'local'" in log


def test_local_mode_with_sso_middleware_set_is_refused(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "local", "SSO_MIDDLEWARE": "authentik@file"})
    assert "AUTH_MODE is 'local'" in log and "SSO_MIDDLEWARE" in log


def test_local_mode_with_sso_middleware_never_prints_the_value(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "local", "SSO_MIDDLEWARE": "authentik-canary-value@file"})
    assert "authentik-canary-value@file" not in log


# --- the disableAuth toggle is wired, gated, and fails closed ---------------

def test_disable_auth_toggle_is_gated_on_first_setup_having_finished(base_out):
    # the render harness's fake filesystem has no kuma.db, so the toggle
    # must be unconditionally gated on it: it must never run against an
    # instance mid first-run setup wizard, whether in this harness or live.
    assert not (base_out / "_copy/var/data/skpulse-prod/uptime-kuma/kuma.db").exists()
    pb = (ROLE / "deploy_skpulse-prod.yml").read_text()
    block = pb.split("Align Kuma's own login")[1].split("tags:")[0]
    assert "skpulse_db_stat.stat.exists" in block
    assert "skpulse_user_count.stdout" in block


def test_disable_auth_toggle_sets_true_for_sso_false_for_local():
    pb = (ROLE / "deploy_skpulse-prod.yml").read_text()
    block = pb.split("Align Kuma's own login")[1].split("- name:")[0]
    assert "'true' if skp_auth_mode == 'sso' else 'false'" in block
    assert "disableAuth" in block and "ON CONFLICT(key)" in block


# --- static -------------------------------------------------------------

def test_env_playbooks_differ_only_in_env_and_subnets():
    def norm(text):
        text = re.sub(r"(?<![A-Za-z])(dev|staging|prod)(?![A-Za-z])", "ENV", text)
        return re.sub(r"\d+\.\d+\.\d+\.\d+/\d+", "CIDR", text)
    prod = norm((ROLE / "deploy_skpulse-prod.yml").read_text())
    for env in ("dev", "staging"):
        assert norm((ROLE / f"deploy_skpulse-{env}.yml").read_text()) == prod, env


@pytest.mark.parametrize("env,host,subnet", [
    ("dev", "skpulse-dev.render.example.test", "172.16.152.0/24"),
    ("staging", "skpulse-staging.render.example.test", "172.16.151.0/24"),
])
def test_dev_and_staging_hosts_and_subnets(tmp_path, env, host, subnet):
    out = ok(tmp_path, BASE, env)
    lab = labels(compose(out, env)["services"]["skpulse"])
    router = f"skpulse-{env}"
    assert lab[f"traefik.http.routers.{router}.rule"] == f"Host(`{host}`)"
    assert lab[f"traefik.http.routers.{router}-public.priority"] == "100"
    assert subnet in (ROLE / f"deploy_skpulse-{env}.yml").read_text()


def test_readme_documents_sign_in_modes_and_bypass_router():
    text = (ROLE / "README.md").read_text()
    for needle in ("AUTH_MODE", "SSO_MIDDLEWARE", "disableAuth", "/socket.io/", "/api/push/"):
        assert needle in text, needle
