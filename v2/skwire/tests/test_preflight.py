"""
Pre-flight: with consent, scan the env → tailored suggestions + the
"deploy here or elsewhere?" question. The probe is injectable so we can test the
logic without touching the real system.
"""
from __future__ import annotations

from skwire.preflight import probe_env, suggest, EnvProfile, FakeProbe


def _beefy():
    return FakeProbe(os="linux", cpu_cores=16, ram_gb=32, gpus=["RTX 4080"],
                     disk_free_gb=500, has_docker=True, has_kubectl=False,
                     has_ollama=True, public_ip=None, free_ports={80, 443})


def _lean():
    return FakeProbe(os="linux", cpu_cores=2, ram_gb=4, gpus=[],
                     disk_free_gb=20, has_docker=False, has_kubectl=False,
                     has_ollama=False, public_ip="1.2.3.4", free_ports={80, 443})


def test_probe_returns_profile():
    p = probe_env(_beefy())
    assert isinstance(p, EnvProfile)
    assert p.ram_gb == 32 and p.gpus == ["RTX 4080"] and p.has_docker is True


def test_always_asks_deploy_here_or_elsewhere():
    out = suggest(probe_env(_beefy()))
    questions = [s.text.lower() for s in out if s.kind == "question"]
    assert any("here" in q and "else" in q for q in questions)


def test_beefy_env_suggests_ai_guided_and_swarm():
    out = [s.text.lower() for s in suggest(probe_env(_beefy()))]
    joined = " ".join(out)
    assert "ai-guided" in joined or "local model" in joined     # has GPU + RAM
    assert "swarm" in joined                                    # docker, single node


def test_lean_env_suggests_install_docker_and_hosted_fallback():
    out = [s.text.lower() for s in suggest(probe_env(_lean()))]
    joined = " ".join(out)
    assert "docker" in joined                                   # not installed
    assert "hosted" in joined or "lean" in joined               # low RAM → fallback


def test_existing_k8s_suggests_deploying_there():
    probe = _beefy(); probe.has_kubectl = True
    out = [s.text.lower() for s in suggest(probe_env(probe))]
    assert any("kubernetes" in t or "k8s" in t or "cluster" in t for t in out)


def test_systemprobe_is_cross_platform_safe():
    # Must not crash on any OS (POSIX sysconf is not available on Windows) and must
    # return numeric ram/disk regardless of platform.
    from skwire.preflight import SystemProbe
    sp = SystemProbe()
    assert isinstance(sp.ram_gb, (int, float))
    assert isinstance(sp.disk_free_gb, (int, float))
    assert isinstance(sp.cpu_cores, int) and sp.cpu_cores >= 1
    assert isinstance(sp.has_docker, bool)


def test_no_public_ip_suggests_exposure_overlay():
    out = [s.text.lower() for s in suggest(probe_env(_beefy()))]   # public_ip=None
    joined = " ".join(out)
    assert "cloudflared" in joined or "pangolin" in joined or "tailscale" in joined
