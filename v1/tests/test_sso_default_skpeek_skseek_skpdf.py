"""skpeek (SearXNG), skseek (Perplexica) and skpdf (Stirling PDF) are behind
sksso forward auth by default.

None of the three has a login of its own. On one production instance they
were open to the internet and skseek's GET /api/config returned the model
provider API keys (operator decision, 2026-11-20). Each router now appends
`<svc>.SSO_MIDDLEWARE` (default `authentik@file`) to the END of its chain,
whether TRAEFIK_MIDDLEWARES is the default or an instance override, and never
twice. `none` or `false` opts out for a deliberately public instance; empty or
null keeps the default (fail closed).
"""
import pathlib

import jinja2
import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
OPTIONAL = V1 / "ansible/optional"
RENDER_VARS = pathlib.Path(__file__).resolve().parent / "render/vars"
ENVS = ("dev", "staging", "prod")
SERVICES = ("skpeek", "skseek", "skpdf")
SSO = "authentik@file"


def _sfx(env):
    return "" if env == "prod" else f"-{env}"


def default_chain(svc, env):
    return f"default-no-crowdsec@file,skpdf{_sfx(env)}-upload" if svc == "skpdf" else "default-no-crowdsec@file"


def render(svc, env="prod", **over):
    e = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    e.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    tpl = (OPTIONAL / svc / f"src/config/{svc}/{svc}.yml.j2").read_text()
    svc_vars = yaml.safe_load((RENDER_VARS / f"{svc}.example.yml").read_text())[svc]
    return e.from_string(tpl).render(
        app=svc, env=env, domain="example.test", cluster_name="render", **{svc: dict(svc_vars, **over)})


def routers(text):
    """{router name: middlewares value} for every HTTP router in the stack."""
    out = {}
    for s in (yaml.safe_load(text).get("services") or {}).values():
        for label in (s.get("deploy") or {}).get("labels") or []:
            key, _, val = label.partition("=")
            parts = key.split(".")
            if key.startswith("traefik.http.routers.") and parts[-1] == "middlewares":
                out[parts[3]] = val
    return out


def chain(svc, env="prod", **over):
    r = routers(render(svc, env, **over))
    main = f"{svc}{_sfx(env)}"
    # The user-facing router plus its error-pages-free API router (#410). The
    # API router derives from the resolved chain, so it ends in the same SSO
    # middleware: skseek's /api/config must never skip sign-in.
    assert sorted(r) == sorted([main, f"{main}-api"]), r
    sso = lambda c: [m.strip() for m in c.split(",") if "authentik" in m]
    assert sso(r[f"{main}-api"]) == sso(r[main]), r
    return r[main].split(",")


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("svc", SERVICES)
def test_default_puts_sso_last(svc, env):
    assert chain(svc, env) == default_chain(svc, env).split(",") + [SSO]


@pytest.mark.parametrize("svc", SERVICES)
def test_every_router_of_the_service_carries_sso(svc):
    # guard for a router added later (upload, api, websocket): it must be
    # protected too, or listed here with the reason it is machine-only.
    for env in ENVS:
        for name, mw in routers(render(svc, env)).items():
            assert mw.split(",")[-1] == SSO, name


@pytest.mark.parametrize("svc", SERVICES)
def test_instance_override_still_gets_sso(svc):
    over = "crowdsec-bouncer@file,default-no-crowdsec@file"
    assert chain(svc, TRAEFIK_MIDDLEWARES=over) == over.split(",") + [SSO]


@pytest.mark.parametrize("svc", SERVICES)
@pytest.mark.parametrize("off", ("none", "None", "false", False, " none "))
def test_opt_out_removes_sso(svc, off):
    assert chain(svc, SSO_MIDDLEWARE=off) == default_chain(svc, "prod").split(",")
    over = "crowdsec-bouncer@file,default@file"
    assert chain(svc, TRAEFIK_MIDDLEWARES=over, SSO_MIDDLEWARE=off) == over.split(",")


@pytest.mark.parametrize("svc", SERVICES)
@pytest.mark.parametrize("unset", ("", None))
def test_empty_or_null_keeps_the_default(svc, unset):
    assert render(svc, SSO_MIDDLEWARE=unset) == render(svc)


@pytest.mark.parametrize("svc", SERVICES)
@pytest.mark.parametrize("over", (
    "authentik@file,default-no-crowdsec@file",
    "crowdsec-bouncer@file,default-no-crowdsec@file,authentik@file",
    "crowdsec-bouncer@file, authentik@file ,default@file",
))
def test_never_duplicated_when_the_chain_already_has_it(svc, over):
    got = chain(svc, TRAEFIK_MIDDLEWARES=over)
    assert got == over.split(",")  # left exactly as the instance wrote it
    assert [m.strip() for m in got].count(SSO) == 1


@pytest.mark.parametrize("svc", SERVICES)
def test_custom_sso_middleware(svc):
    assert chain(svc, SSO_MIDDLEWARE="authentik-nam@file")[-1] == "authentik-nam@file"
    assert SSO not in chain(svc, SSO_MIDDLEWARE="authentik-nam@file")


@pytest.mark.parametrize("svc", SERVICES)
def test_only_the_middlewares_label_changes(svc):
    on = render(svc).splitlines()
    off = render(svc, SSO_MIDDLEWARE="none").splitlines()
    assert len(on) == len(off)
    diff = [(a, b) for a, b in zip(on, off) if a != b]
    # the user-facing router and its API router (#410), nothing else
    assert len(diff) == 2 and all(a.rstrip().endswith(f',{SSO}"') for a, _ in diff), diff


@pytest.mark.parametrize("svc", SERVICES)
def test_docs_name_the_knob(svc):
    catalog = (V1.parent / "docs/v1-service-catalog.md").read_text()
    assert f"{svc}.SSO_MIDDLEWARE" in catalog
    readme = OPTIONAL / svc / "README.md"
    if readme.exists():
        assert "SSO_MIDDLEWARE" in readme.read_text()
