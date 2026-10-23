"""skfenceha.DROP_FORWARDED_HEADERS: request headers removed on the public
(websecure) entrypoint before any router sees them.

Why: behind a tunnel or CDN the instance sets skfenceha.TRUSTED_PROXIES so
Traefik keeps the edge's X-Forwarded-For (the real client). Traefik trusts
the whole X-Forwarded-* set from those sources, and an edge such as
Cloudflare passes a client-sent X-Forwarded-Host through unchanged. On a
production cluster Authentik then rendered `api.base: "https://<forged
host>/"` into its flow page (gorilla ProxyHeaders sets the request host from
X-Forwarded-Host), the classic password-reset link poisoning setup. With
`DROP_FORWARDED_HEADERS: [X-Forwarded-Host]` the header is gone before the
routers, so apps fall back to the Host header, which Traefik routed on.
Traefik's own forwardAuth still sends the real host to auth services.

Unset or empty: byte-identical render (no entrypoint middleware, no
middleware definition)."""
import pathlib

import jinja2
import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
SVC = V1 / "ansible/core/skfenceha"
CFG = SVC / "src/config/skfenceha"
WORKER = CFG / "traefik-worker.yml.j2"
ACME = CFG / "traefik.yml.j2"
MIDDLEWARES = CFG / "dynamic-middlewares.yml.j2"
PLAYBOOKS = [SVC / f"deploy_skfenceha-{e}.yml" for e in ("dev", "staging", "prod")]


def _env():
    return jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)


def _worker_text(**o):
    return _env().from_string(WORKER.read_text()).render(env="prod", app="skfenceha", skfenceha={"ACME_ENABLED": False, **o})


def _mw_text(**o):
    return _env().from_string(MIDDLEWARES.read_text()).render(skfenceha=o)


def _eps(**o):
    return yaml.safe_load(_worker_text(**o))["entryPoints"]


def _mws(**o):
    return yaml.safe_load(_mw_text(**o))["http"]["middlewares"]


@pytest.mark.parametrize("value", [[], None])
def test_unset_or_empty_renders_byte_identical(value):
    o = {} if value is None else {"DROP_FORWARDED_HEADERS": value}
    assert _worker_text(**o) == _worker_text()
    assert _mw_text(**o) == _mw_text()
    # http.encodedCharacters is now always present (Traefik v3.7 explicit
    # defaults, unrelated to this knob); only the drop-forwarded-headers
    # middleware itself must still be absent.
    assert "middlewares" not in _eps(**o)["websecure"].get("http", {})
    assert "drop-forwarded-headers" not in _mws(**o)


def test_websecure_gets_the_entrypoint_middleware():
    eps = _eps(DROP_FORWARDED_HEADERS=["X-Forwarded-Host"], TRUSTED_PROXIES=["172.18.0.1/32"])
    assert eps["websecure"]["http"]["middlewares"] == ["drop-forwarded-headers@file"]
    assert eps["websecure"]["http"]["encodedCharacters"]["allowEncodedSlash"] is True
    assert eps["websecure"]["forwardedHeaders"] == {"trustedIPs": ["172.18.0.1/32"]}
    assert eps["websecure"]["address"] == ":443"


def test_web_entrypoint_keeps_only_its_redirect():
    eps = _eps(DROP_FORWARDED_HEADERS=["X-Forwarded-Host"])
    # web never carries the drop-forwarded-headers middleware (websecure
    # only); encodedCharacters is always present alongside its own redirect.
    assert "middlewares" not in eps["web"]["http"]
    assert eps["web"]["http"]["redirections"] == {"entryPoint": {"to": "websecure", "scheme": "https", "permanent": True}}


def test_middleware_removes_each_listed_header():
    mws = _mws(DROP_FORWARDED_HEADERS=["X-Forwarded-Host", "X-Forwarded-Prefix"])
    assert mws["drop-forwarded-headers"] == {"headers": {"customRequestHeaders": {"X-Forwarded-Host": "", "X-Forwarded-Prefix": ""}}}


def test_coexists_with_responding_timeouts():
    eps = _eps(DROP_FORWARDED_HEADERS=["X-Forwarded-Host"], WORKER_RESPONDING_TIMEOUTS={"readTimeout": "7200s"})
    assert eps["websecure"]["http"]["middlewares"] == ["drop-forwarded-headers@file"]
    assert eps["websecure"]["transport"] == {"respondingTimeouts": {"readTimeout": "7200s"}}


def test_acme_traefik_has_no_entrypoint_middleware():
    out = _env().from_string(ACME.read_text()).render(env="prod", app="skfenceha", skfenceha={"ACME_ENABLED": False, "DROP_FORWARDED_HEADERS": ["X-Forwarded-Host"]})
    # The ACME traefik never carries the drop-forwarded-headers middleware
    # (that is a worker-only concern); encodedCharacters is always present.
    assert "middlewares" not in yaml.safe_load(out)["entryPoints"]["websecure"].get("http", {})


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_playbooks_validate_the_header_names_after_loading_the_vault(pb):
    plays = yaml.safe_load(pb.read_text())
    play = next(p for p in plays if p.get("hosts") == "selected_manager_group")
    names = [t.get("name", "") for t in play["pre_tasks"]]
    load = names.index("Load vault file")
    check = next(i for i, n in enumerate(names) if "DROP_FORWARDED_HEADERS" in n)
    assert check > load
    body = yaml.safe_dump(play["pre_tasks"][check])
    assert "DROP_FORWARDED_HEADERS" in body and "match" in body


def test_readme_documents_the_knob():
    readme = (SVC / "README.md").read_text()
    assert "DROP_FORWARDED_HEADERS" in readme
    assert "X-Forwarded-Host" in readme
