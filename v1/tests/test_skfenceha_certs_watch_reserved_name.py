"""certs-generated.yml is a framework-owned dynamic file name, same as
routers.yml/services.yml/middlewares.yml/tls.yml: it's traefik-certs-watch's
own managed output (see src/config/skfenceha/certs-watch.sh), written
directly into skfenceha's dynamic/ directory. An instance's own
EXTRA_DYNAMIC_FILES must not be able to collide with it.

skfence has no traefik-certs-watch sidecar (single-node, no restart-free
reload problem to solve), so its reserved list is unchanged.
"""
import pathlib

import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
ANSIBLE = V1 / "ansible"


def _tasks(path):
    plays = yaml.safe_load(path.read_text())
    return [t for p in plays if isinstance(p, dict) for t in p.get("tasks", [])]


def _extra_dynamic_vars(svc, env):
    tasks = _tasks(ANSIBLE / f"core/{svc}/deploy_{svc}-{env}.yml")
    hits = [t for t in tasks
            if "render_extra_dynamic_files.yml" in str(t.get("include_tasks", t.get("import_tasks", "")))]
    assert len(hits) == 1, f"expected exactly one EXTRA_DYNAMIC_FILES task in {svc}-{env}"
    return hits[0]["vars"]


def test_skfenceha_reserves_certs_generated_yml_in_every_env():
    for env in ("dev", "staging", "prod"):
        reserved = set(_extra_dynamic_vars("skfenceha", env)["extra_dynamic_reserved"])
        assert "certs-generated.yml" in reserved, env


def test_skfence_does_not_reserve_certs_generated_yml():
    """skfence never writes certs-generated.yml, so reserving the name
    there would only confuse instance authors with no benefit."""
    for env in ("dev", "staging", "prod"):
        reserved = set(_extra_dynamic_vars("skfence", env)["extra_dynamic_reserved"])
        assert "certs-generated.yml" not in reserved, env


def test_skfenceha_deploys_certs_watch_script_in_every_env():
    for env in ("dev", "staging", "prod"):
        tasks = _tasks(ANSIBLE / f"core/skfenceha/deploy_skfenceha-{env}.yml")
        hits = [t for t in tasks if "certs-watch.sh" in str(t.get("copy", {}))]
        assert len(hits) == 1, f"expected exactly one certs-watch.sh deploy task in {env}"
        copy = hits[0]["copy"]
        assert copy["mode"] == "0755"
        assert copy["src"].endswith("certs-watch.sh")
        assert copy["dest"].endswith("/certs-watch.sh")
