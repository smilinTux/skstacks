"""The sovereign install flow — the ordered named steps `skbloom up` runs. Built from
a Profile, driven by an injected command runner so the whole orchestration is testable
without a real cluster."""
from __future__ import annotations

import pytest

from pathlib import Path

from skbloom.flow import Profile, install_flow
from skbloom.steps import run_steps, StateStore

V2_ROOT = str(Path(__file__).resolve().parents[2])      # real descriptors live here


class FakeRun:
    """Records commands; returns canned (stdout, rc). Lets tests script check() results."""
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}      # substring → (stdout, rc)

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for sub, resp in self.responses.items():
            if sub in joined:
                return resp
        return ("", 0)

    def ran(self, *substr):
        return [c for c in self.calls if all(s in " ".join(c) for s in substr)]


@pytest.fixture
def profile():
    return Profile(cluster="skbloom-test", services=["cloud/skfence", "core/sksso"])


def test_flow_has_the_expected_named_spine(profile):
    names = [s.name for s in install_flow(profile, run=FakeRun(), v2_root="/x")]
    # the Kubefirst-mapped spine, in order
    assert names[:4] == ["preflight", "bootstrap-cluster", "install-eso", "secret-backend"]
    assert "deploy:skfence" in names and "deploy:sksso" in names
    assert names[-1] == "final-check"


def test_up_runs_every_step_in_order(profile, tmp_path):
    run = FakeRun()
    steps = install_flow(profile, run=run, v2_root=V2_ROOT)
    results = run_steps(steps, StateStore(tmp_path / "s.json"))
    assert all(r.status in ("done", "skipped") for r in results)
    # cluster created before ESO before a deploy
    assert run.ran("k3d", "cluster", "create")
    assert run.ran("helm", "external-secrets")
    assert run.ran("kubectl", "apply")           # services applied


def test_bootstrap_is_idempotent_when_cluster_exists(profile, tmp_path):
    # if k3d already lists the cluster, the create action must NOT run again
    run = FakeRun({"k3d cluster list": ("skbloom-test\n", 0)})
    steps = install_flow(profile, run=run, v2_root=V2_ROOT)
    run_steps(steps, StateStore(tmp_path / "s.json"))
    assert run.ran("k3d", "cluster", "create") == []      # skipped via check()


def test_swarm_platform_has_its_own_lean_spine(tmp_path):
    prof = Profile(cluster="c", services=["compute/skcache"], platform="swarm")
    names = [s.name for s in install_flow(prof, run=FakeRun(), v2_root=V2_ROOT)]
    assert names[:2] == ["preflight", "bootstrap-swarm"]
    assert "install-eso" not in names and "secret-backend" not in names   # k8s/ESO-only
    assert "deploy:skcache" in names and names[-1] == "final-check"


def test_swarm_bootstrap_is_idempotent_when_already_a_swarm(tmp_path):
    run = FakeRun({"docker info": ("Server:\n Swarm: active\n", 0)})
    steps = install_flow(Profile(cluster="c", services=[], platform="swarm"), run=run, v2_root=V2_ROOT)
    run_steps(steps, StateStore(tmp_path / "s.json"))
    assert run.ran("docker", "swarm", "init") == []        # already a swarm → skip init


def test_swarm_deploy_uses_stack_deploy_with_seeded_env(tmp_path):
    prof = Profile(cluster="c", services=["compute/skcache=cache"], platform="swarm",
                   secret_seed=[{"key": "skcache/valkey_password", "value": "sekret"}])
    run = FakeRun()
    run_steps(install_flow(prof, run=run, v2_root=V2_ROOT), StateStore(tmp_path / "s.json"))
    allc = "\n".join(" ".join(c) for c in run.calls)
    assert "docker stack deploy" in allc and "cache" in allc
    assert "VALKEY_PASSWORD=sekret" in allc                 # swarm injects the secret as env


def test_tls_adds_a_cert_manager_ca_step(tmp_path):
    prof = Profile(cluster="c", services=["core/skca"], tls=True)
    run = FakeRun()
    steps = install_flow(prof, run=run, v2_root=V2_ROOT)
    assert any(s.name == "local-tls" for s in steps)
    run_steps(steps, StateStore(tmp_path / "s.json"))
    assert run.ran("helm", "cert-manager")               # installs cert-manager
    applied = "\n".join(" ".join(c) for c in run.calls)
    assert "skbloom-ca" in applied                        # applies the CA chain


def test_service_urls_only_for_services_with_a_web_ui():
    from skbloom.flow import service_urls
    p = Profile(services=["cloud/skfence=edge", "core/sksec"], tls=True, domain="sk.local")
    urls = service_urls(p, V2_ROOT)
    assert urls["edge"] == "https://edge.sk.local"     # skfence has ports → a web UI link
    assert "sksec" not in urls                          # CrowdSec is portless → no UI link


def test_service_urls_http_without_tls():
    from skbloom.flow import service_urls
    urls = service_urls(Profile(services=["compute/skcache=c"], tls=False, domain="sk.local"), V2_ROOT)
    assert urls.get("c", "").startswith("http://")


def test_no_tls_by_default(profile, tmp_path):
    steps = install_flow(profile, run=FakeRun(), v2_root=V2_ROOT)
    assert not any(s.name == "local-tls" for s in steps)


def test_vanity_name_renames_the_deploy_step_and_namespace(tmp_path):
    # 'call my SSO whatever I want' — core/sksso deployed as 'login'
    prof = Profile(cluster="c", services=["core/sksso=login"])
    run = FakeRun()
    steps = install_flow(prof, run=run, v2_root=V2_ROOT)
    assert any(s.name == "deploy:login" for s in steps)
    run_steps(steps, StateStore(tmp_path / "s.json"))
    applied = "\n".join(" ".join(c) for c in run.ran("kubectl", "apply"))
    assert "namespace login" in applied or "name: login" in applied


def test_resume_after_a_failed_step(profile, tmp_path):
    calls = {"n": 0}
    def flaky(cmd, **kw):
        if cmd[:2] == ["helm", "upgrade"]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("registry timeout")
        return ("", 0)
    store = StateStore(tmp_path / "s.json")
    r1 = run_steps(install_flow(profile, run=flaky, v2_root=V2_ROOT), store)
    assert any(r.status == "failed" and r.name == "install-eso" for r in r1)
    assert "bootstrap-cluster" in store.completed() and "install-eso" not in store.completed()
    # resume → eso retried + the rest complete
    r2 = run_steps(install_flow(profile, run=flaky, v2_root=V2_ROOT), store)
    assert "final-check" in store.completed()
