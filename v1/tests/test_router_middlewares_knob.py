"""Per-router Traefik middleware override knobs.

Ten optional services hardcoded their router chain to
``default-no-crowdsec@file`` (skpdf and skvector with their own upload or
buffering middleware in front or behind it). Putting one of them behind sksec's
``crowdsec-bouncer@file`` meant hand-editing live service labels, which the
next deploy silently reverted. Each router now reads one vault key, the same
convention as ``skgallery.TRAEFIK_MIDDLEWARES``:

  <svc>.TRAEFIK_MIDDLEWARES        the main router's whole middleware string
  skvector.TRAEFIK_MIDDLEWARES_GRPC  skvector's gRPC router

Unset (or empty) renders byte-identical to skstacks-v2.26.4: the baselines in
fixtures/router_middlewares_baseline/ were rendered from the v2.26.4 templates
with the render gate's example vars, before this change.
"""
import pathlib
import re

import jinja2
import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
REPO = V1.parent
OPTIONAL = V1 / "ansible/optional"
BASELINE = pathlib.Path(__file__).resolve().parent / "fixtures/router_middlewares_baseline"
RENDER_VARS = pathlib.Path(__file__).resolve().parent / "render/vars"
CATALOG = REPO / "docs/v1-service-catalog.md"
ENVS = ("dev", "staging", "prod")


def _sfx(env):
    return "" if env == "prod" else f"-{env}"


# service -> {router name suffix after "<svc><-env>": (knob, default chain(env))}
SERVICES = {
    "skgit": {"": ("TRAEFIK_MIDDLEWARES", lambda e: "default-no-crowdsec@file")},
    "skorch": {"": ("TRAEFIK_MIDDLEWARES", lambda e: "default-no-crowdsec@file")},
    "skpdf": {"": ("TRAEFIK_MIDDLEWARES", lambda e: f"default-no-crowdsec@file,skpdf{_sfx(e)}-upload")},
    "skpeek": {"": ("TRAEFIK_MIDDLEWARES", lambda e: "default-no-crowdsec@file")},
    "skport": {"": ("TRAEFIK_MIDDLEWARES", lambda e: "default-no-crowdsec@file")},
    "skseek": {"": ("TRAEFIK_MIDDLEWARES", lambda e: "default-no-crowdsec@file")},
    "skstor": {"": ("TRAEFIK_MIDDLEWARES", lambda e: "default-no-crowdsec@file")},
    "skvector": {
        "": ("TRAEFIK_MIDDLEWARES", lambda e: f"skvector{_sfx(e)}-buffering,default-no-crowdsec@file"),
        "-grpc": ("TRAEFIK_MIDDLEWARES_GRPC", lambda e: "default-no-crowdsec@file"),
    },
    "skpulse": {"": ("TRAEFIK_MIDDLEWARES", lambda e: "default-no-crowdsec@file")},
    "skboard": {"": ("TRAEFIK_MIDDLEWARES", lambda e: "default-no-crowdsec@file")},
}
CASES = [(svc, sfx) for svc, routers in SERVICES.items() for sfx in routers]
OVERRIDE = "crowdsec-bouncer@file,default@file"


def compose_path(svc):
    return OPTIONAL / svc / f"src/config/{svc}/{svc}.yml.j2"


def example_vars(svc):
    return yaml.safe_load((RENDER_VARS / f"{svc}.example.yml").read_text())[svc]


def _env():
    e = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    e.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return e


# skorch's templates read the combined fact `skorch_cfg` (vault `skorch.*` over
# the play's defaults; see test_skorch_extra_vars_precedence.py), never `skorch`.
CONTEXT_NAME = {"skorch": "skorch_cfg"}


def render(svc, env="prod", **over):
    return _env().from_string(compose_path(svc).read_text()).render(
        app=svc, env=env, domain="example.test", cluster_name="render",
        **{CONTEXT_NAME.get(svc, svc): dict(example_vars(svc), **over)})


def router_middlewares(text, svc, env, sfx):
    """Every value of the router's `.middlewares=` label, across services."""
    key = f"traefik.http.routers.{svc}{_sfx(env)}{sfx}.middlewares"
    found = []
    for s in (yaml.safe_load(text).get("services") or {}).values():
        labels = (s.get("deploy") or {}).get("labels") or s.get("labels") or []
        if isinstance(labels, dict):
            labels = [f"{k}={v}" for k, v in labels.items()]
        found += [l.split("=", 1)[1] for l in labels if l.split("=", 1)[0] == key]
    return found


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("svc", SERVICES)
def test_unset_knobs_render_byte_identical_to_v2_26_4(svc, env):
    assert render(svc, env) == (BASELINE / f"{svc}-{env}.yml").read_text()


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("svc,sfx", CASES)
def test_default_chain_is_the_old_hardcoded_one(svc, sfx, env):
    _, default = SERVICES[svc][sfx]
    assert router_middlewares(render(svc, env), svc, env, sfx) == [default(env)]


@pytest.mark.parametrize("svc", SERVICES)
def test_empty_knobs_render_byte_identical_to_unset(svc):
    empty = {knob: "" for knob, _ in SERVICES[svc].values()}
    for env in ENVS:
        assert render(svc, env, **empty) == render(svc, env)


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("svc,sfx", CASES)
def test_override_replaces_only_that_routers_chain(svc, sfx, env):
    knob, _ = SERVICES[svc][sfx]
    text = render(svc, env, **{knob: OVERRIDE})
    assert router_middlewares(text, svc, env, sfx) == [OVERRIDE]
    for other, (_, default) in SERVICES[svc].items():
        if other != sfx:
            assert router_middlewares(text, svc, env, other) == [default(env)]
    # nothing else in the stack moves: only that one label line differs.
    # Exception: putting crowdsec-bouncer on skgit's main router also renders the
    # skgit runner RPC router (test_skgit_runner_rpc_router_follows_the_crowdsec_knob).
    old = render(svc, env).splitlines()
    new = [l for l in text.splitlines() if not (svc == "skgit" and "-runner-rpc." in l)]
    assert len(old) == len(new)
    diff = [(a, b) for a, b in zip(old, new) if a != b]
    assert len(diff) == 1 and diff[0][1].rstrip().endswith(f'.middlewares={OVERRIDE}"'), diff


@pytest.mark.parametrize("svc", SERVICES)
def test_no_router_hardcodes_default_no_crowdsec(svc):
    text = compose_path(svc).read_text()
    assert not re.search(r"\.middlewares=default-no-crowdsec@file", text), svc


def test_catalog_documents_every_knob():
    text = CATALOG.read_text()
    for svc, routers in SERVICES.items():
        for knob, _ in routers.values():
            assert f"{svc}.{knob}" in text, f"{svc}.{knob} missing from {CATALOG.name}"


@pytest.mark.parametrize("svc", SERVICES)
def test_readme_vault_section_documents_the_knob_where_one_exists(svc):
    readme = OPTIONAL / svc / "README.md"
    if not readme.exists():
        pytest.skip("no README: the catalog documents it")
    text = readme.read_text()
    for knob, _ in SERVICES[svc].values():
        assert knob in text, f"{svc}/README.md does not mention {knob}"


RUNNER_RPC_LIVE = [
    "traefik.http.routers.skgit-runner-rpc.rule=Host(`skgit.render.example.test`) && PathPrefix(`/api/actions/runner.v1.RunnerService/`)",
    "traefik.http.routers.skgit-runner-rpc.entrypoints=websecure",
    "traefik.http.routers.skgit-runner-rpc.tls=true",
    "traefik.http.routers.skgit-runner-rpc.tls.certresolver=main",
    "traefik.http.routers.skgit-runner-rpc.tls.options=intermediate@file",
    "traefik.http.routers.skgit-runner-rpc.priority=200",
    "traefik.http.routers.skgit-runner-rpc.middlewares=default-no-crowdsec@file",
    "traefik.http.routers.skgit-runner-rpc.service=skgit",
]


def _skgit_runner_labels(text):
    labels = []
    for s in (yaml.safe_load(text).get("services") or {}).values():
        labels += [l for l in ((s.get("deploy") or {}).get("labels") or []) if "-runner-rpc." in l]
    return labels


def test_skgit_runner_rpc_router_follows_the_crowdsec_knob():
    """Runner RPC bypasses the CrowdSec forward-auth only when the main router uses it.

    A slow CrowdSec LAPI makes the bouncer fail closed with 403, which starves
    Forgejo runners; runner RPC is still authenticated by the runner token.
    """
    assert _skgit_runner_labels(render("skgit", "prod")) == []
    with_bouncer = render("skgit", "prod", TRAEFIK_MIDDLEWARES=OVERRIDE)
    host = re.search(r"skgit\.rule=Host\(`([^`]+)`\)", with_bouncer).group(1)
    expected = [l.replace("skgit.render.example.test", host) for l in RUNNER_RPC_LIVE]
    assert _skgit_runner_labels(with_bouncer) == expected
    kept = render("skgit", "prod", TRAEFIK_MIDDLEWARES=OVERRIDE, RUNNER_RPC_MIDDLEWARES=OVERRIDE)
    assert f"traefik.http.routers.skgit-runner-rpc.middlewares={OVERRIDE}" in _skgit_runner_labels(kept)
