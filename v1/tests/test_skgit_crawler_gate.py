"""skgit crawler gate (Anubis) and the Forgejo config it depends on.

Crawlers rotating Chrome user agents over many IPs walked Forgejo's history
views (blame, commits, raw, archive: each runs git over the full history),
and concurrent repacks then took Forgejo past its memory limit. The role now:

- puts Anubis (proof-of-work) in front of the web UI only, on by default
  (skgit.ANUBIS_ENABLED: false turns it off), while a direct router sends
  every non-browser path (git smart HTTP, LFS, /api/, OAuth/OIDC, assets,
  robots.txt, /metrics) straight to Forgejo, and the registry and runner RPC
  routers keep their own priorities;
- makes app.ini.j2 the only Forgejo configuration: the service runs
  `forgejo web` directly, so FORGEJO__* environment never reached app.ini
  (the reverse proxy trust and session settings in the compose were dead);
- ships bounded git.config pack limits, a robots.txt, and a long healthcheck
  start period (a major upgrade's migrations outlived 300s on NFS).
"""
import configparser
import pathlib
import re

import jinja2
import pytest
import yaml

from image_pins import is_pinned, template_default

V1 = pathlib.Path(__file__).resolve().parents[1]
ROLE = V1 / "ansible/optional/skgit"
CFG = ROLE / "src/config/skgit"
COMPOSE = CFG / "skgit.yml.j2"
APP_INI = CFG / "app.ini.j2"
SKGIT_ENV = CFG / "skgit.env.j2"
ROBOTS = CFG / "robots.txt"
PLAYBOOKS = [ROLE / f"deploy_skgit-{e}.yml" for e in ("dev", "staging", "prod")]
ENVS = ("dev", "staging", "prod")
BASE = {"CLUSTERNAME": "render", "DOMAIN": "example.test"}
BOUNCER = "crowdsec-bouncer@file,default@file"


def _env():
    e = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    e.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return e


def _render(path, env="prod", **over):
    secrets = {k: f"example-{k.lower()}" for k in ("POSTGRES_PASSWORD", "SECRET_KEY", "INTERNAL_TOKEN", "SHARED_SECRET")}
    return _env().from_string(path.read_text()).render(env=env, app="skgit", skgit={**BASE, **secrets, **over})


def compose(env="prod", **over):
    return yaml.safe_load(_render(COMPOSE, env, **over))


def labels(doc, svc):
    return (doc["services"][svc].get("deploy") or {}).get("labels") or []


def label(doc, svc, key):
    found = [l.split("=", 1)[1] for l in labels(doc, svc) if l.split("=", 1)[0] == key]
    assert len(found) <= 1, (key, found)
    return found[0] if found else None


def _r(env):
    return "skgit" if env == "prod" else f"skgit-{env}"


def ini(**over):
    cp = configparser.ConfigParser(interpolation=None, strict=True)
    cp.optionxform = str
    cp.read_string("[_root]\n" + _render(APP_INI, **over))
    return cp


# ---------------------------------------------------------------- routing


@pytest.mark.parametrize("env", ENVS)
def test_anubis_is_on_by_default_and_fronts_the_web_router(env):
    doc = compose(env)
    r = _r(env)
    assert "anubis" in doc["services"]
    assert label(doc, "forgejo", f"traefik.http.routers.{r}.service") == f"{r}-anubis"
    assert label(doc, "anubis", f"traefik.http.services.{r}-anubis.loadbalancer.server.port") == "8923"
    assert label(doc, "anubis", "traefik.swarm.network") == f"cloud-public-{env}"
    # the web path keeps the operator's middleware chain (CrowdSec included)
    bouncer = compose(env, TRAEFIK_MIDDLEWARES=BOUNCER)
    assert label(bouncer, "forgejo", f"traefik.http.routers.{r}.middlewares") == BOUNCER


@pytest.mark.parametrize("env", ENVS)
def test_disabled_gate_routes_the_web_ui_straight_to_forgejo(env):
    doc = compose(env, ANUBIS_ENABLED=False)
    r = _r(env)
    assert "anubis" not in doc["services"]
    assert label(doc, "forgejo", f"traefik.http.routers.{r}.service") == r
    assert not [l for l in labels(doc, "forgejo") if f"routers.{r}-direct." in l]
    assert compose(env, ANUBIS_ENABLED="false") == doc


@pytest.mark.parametrize("env", ENVS)
def test_direct_router_bypasses_the_gate_with_the_same_chain(env):
    r = _r(env)
    for mw, want in ((None, "default-no-crowdsec@file"), (BOUNCER, BOUNCER)):
        doc = compose(env, **({"TRAEFIK_MIDDLEWARES": mw} if mw else {}))
        assert label(doc, "forgejo", f"traefik.http.routers.{r}-direct.service") == r
        assert label(doc, "forgejo", f"traefik.http.routers.{r}-direct.middlewares") == want
        assert label(doc, "forgejo", f"traefik.http.routers.{r}-direct.entrypoints") == "websecure"
        assert label(doc, "forgejo", f"traefik.http.routers.{r}-direct.tls.certresolver") == "main"


def test_router_priorities_keep_runner_rpc_and_registry_on_top():
    doc = compose(TRAEFIK_MIDDLEWARES=BOUNCER)
    prio = {name: int(label(doc, "forgejo", f"traefik.http.routers.skgit{name}.priority")) for name in ("-registry", "-direct", "-runner-rpc")}
    assert prio["-runner-rpc"] > prio["-direct"] > prio["-registry"]
    # the runner RPC router still skips CrowdSec while the web path keeps it
    assert label(doc, "forgejo", "traefik.http.routers.skgit-runner-rpc.middlewares") == "default-no-crowdsec@file"
    # registry router untouched: /v2 straight to Forgejo, never through Anubis
    assert label(doc, "forgejo", "traefik.http.routers.skgit-registry.service") == "skgit"
    assert label(doc, "forgejo", "traefik.http.routers.skgit-registry.rule").endswith("PathPrefix(`/v2`)")


def _direct_matcher(doc):
    rule = label(doc, "forgejo", "traefik.http.routers.skgit-direct.rule")
    host, paths = re.fullmatch(r"Host\(`([^`]+)`\) && \((.*)\)", rule).groups()
    prefixes = re.findall(r"PathPrefix\(`([^`]+)`\)", paths)
    exact = re.findall(r"Path\(`([^`]+)`\)", paths)
    regexes = [re.compile(x) for x in re.findall(r"PathRegexp\(`([^`]+)`\)", paths)]
    assert len(prefixes) + len(exact) + len(regexes) == paths.count("||") + 1, "unparsed matcher in the rule"
    return host, lambda p: p in exact or any(p.startswith(x) for x in prefixes) or any(x.search(p) for x in regexes)


DIRECT = [
    "/o/r.git/info/refs", "/o/r/info/refs", "/o/r.git/git-upload-pack", "/o/r/git-upload-pack",
    "/o/r.git/git-receive-pack", "/o/r.git/info/lfs/objects/batch", "/o/r/info/lfs/locks",
    "/api/v1/version", "/api/actions/runner.v1.RunnerService/FetchTask", "/api/packages/o/npm/x",
    "/api/healthz", "/user/login", "/user/oauth2/sksso", "/user/oauth2/sksso/callback",
    "/login/oauth/authorize", "/login/oauth/access_token", "/.well-known/openid-configuration",
    "/assets/css/index.css", "/avatars/abc", "/repo-avatars/1-abc", "/robots.txt", "/favicon.ico",
    "/favicon.svg", "/metrics",
]
GATED = [
    "/", "/o", "/o/r", "/o/r/blame/branch/main/README.md", "/o/r/commits/branch/main",
    "/o/r/commit/abc123", "/o/r/src/commit/abc123/x", "/o/r/src/tag/v1/x", "/o/r/raw/branch/main/f",
    "/o/r/archive/main.tar.gz", "/o/r/compare/a...b", "/explore/repos", "/o/r/src/branch/main/info/refs",
    "/user/settings", "/o/r/issues", "/v2",
]


@pytest.mark.parametrize("path", DIRECT)
def test_non_browser_paths_skip_the_gate(path):
    host, direct = _direct_matcher(compose())
    assert host == "skgit.render.example.test"
    assert direct(path), path


@pytest.mark.parametrize("path", GATED)
def test_history_and_page_views_are_gated(path):
    _, direct = _direct_matcher(compose())
    assert not direct(path), path


# ---------------------------------------------------------------- the anubis service


def test_anubis_service_is_pinned_bounded_and_healthchecked():
    text = COMPOSE.read_text()
    assert is_pinned(template_default(text, "ANUBIS_IMAGE"), "ghcr.io/techarohq/anubis", "v1.")
    svc = compose()["services"]["anubis"]
    env = dict(e.split("=", 1) for e in svc["environment"])
    assert env["TARGET"] == "http://skgit-prod_forgejo:3000"
    assert env["REDIRECT_DOMAINS"] == "skgit.render.example.test"
    assert env["SERVE_ROBOTS_TXT"] == "false"
    assert set(svc["networks"]) == {"skgit-prod", "cloud-public-prod"}
    assert svc["healthcheck"]["test"] == ["CMD", "/ko-app/anubis", "--healthcheck"]
    assert svc["deploy"]["resources"]["limits"]["memory"]
    assert compose(placement_constraints=["node.role == worker"])["services"]["anubis"]["deploy"]["placement"]["constraints"] == ["node.role == worker"]
    assert compose(env="dev")["services"]["anubis"]["environment"][2] == "TARGET=http://skgit-dev_forgejo:3000"


def test_anubis_image_is_overridable():
    ref = "registry.example.test/anubis:v1.99.0@sha256:" + "0" * 64
    assert compose(ANUBIS_IMAGE=ref)["services"]["anubis"]["image"] == ref


# ---------------------------------------------------------------- forgejo config


def test_compose_carries_no_inert_forgejo_environment():
    """FORGEJO__* on the service never reached app.ini (no environment-to-ini)."""
    svc = compose()["services"]["forgejo"]
    assert "environment" not in svc
    assert "FORGEJO__" not in "".join(l for l in COMPOSE.read_text().splitlines() if not l.lstrip().startswith("#"))


def test_env_file_cannot_reopen_the_installer():
    out = _render(SKGIT_ENV)
    assert "FORGEJO__security__INSTALL_LOCK=true" in out
    assert "FORGEJO__security__INSTALL_LOCK=false" not in out
    assert "FORGEJO__database__HOST=skgit-prod_postgres:5432" in out


def test_reverse_proxy_trust_and_session_live_in_app_ini():
    cp = ini()
    assert cp["security"]["REVERSE_PROXY_LIMIT"] == "1"
    assert cp["security"]["REVERSE_PROXY_TRUSTED_PROXIES"] == "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    assert ini(REVERSE_PROXY_TRUSTED_PROXIES="172.16.200.0/24")["security"]["REVERSE_PROXY_TRUSTED_PROXIES"] == "172.16.200.0/24"
    assert dict(cp["session"]) == {"PROVIDER": "file", "PROVIDER_CONFIG": "/data/gitea/sessions", "COOKIE_SECURE": "true"}
    # the sessions dir is the one the compose bind-mounts
    vols = compose()["services"]["forgejo"]["volumes"]
    assert "/var/data/runtime/skgit-prod/sessions:/data/gitea/sessions" in vols


def test_git_config_pack_limits_are_role_defaults_and_overridable():
    assert dict(ini()["git.config"]) == {"pack.threads": "1", "pack.windowMemory": "256m", "pack.deltaCacheSize": "64m", "gc.auto": "0"}
    over = dict(ini(GIT_CONFIG={"pack.threads": "2", "core.bigFileThreshold": "16m"})["git.config"])
    assert over == {"pack.windowMemory": "256m", "pack.deltaCacheSize": "64m", "gc.auto": "0", "pack.threads": "2", "core.bigFileThreshold": "16m"}


@pytest.mark.parametrize("knob,section,key", [
    ("ACTIONS_ENABLED", "actions", "ENABLED"),
    ("PACKAGES_ENABLED", "packages", "ENABLED"),
    ("LFS_ENABLED", "lfs", "ENABLED"),
    ("ENABLE_PUSH_CREATE_USER", "repository", "ENABLE_PUSH_CREATE_USER"),
    ("ENABLE_PUSH_CREATE_ORG", "repository", "ENABLE_PUSH_CREATE_ORG"),
])
def test_documented_knobs_now_reach_app_ini(knob, section, key):
    """These knobs used to set only the (inert) env file."""
    assert ini()[section][key] == "true"
    assert ini(**{knob: False})[section][key] == "false"
    assert ini(**{knob: "false"})[section][key] == "false"


def test_health_start_period_is_a_knob_with_a_long_default():
    hc = compose()["services"]["forgejo"]["healthcheck"]
    assert hc["start_period"] == "60m"
    assert compose(HEALTH_START_PERIOD="2h")["services"]["forgejo"]["healthcheck"]["start_period"] == "2h"


# ---------------------------------------------------------------- robots.txt


def test_robots_txt_keeps_crawlers_off_history_views():
    rules = [l.split(":", 1)[1].strip() for l in ROBOTS.read_text().splitlines() if l.startswith("Disallow:")]
    for p in ("/*/*/blame/", "/*/*/commits/", "/*/*/commit/", "/*/*/raw/", "/*/*/archive/", "/*/*/src/commit/"):
        assert p in rules
    assert "/" not in rules  # the code itself stays indexable


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_every_env_playbook_installs_robots_txt_into_custom_public(playbook):
    plays = yaml.safe_load(playbook.read_text())
    tasks = [t for p in plays for t in (p.get("tasks") or [])]
    copy = [t for t in tasks if "robots.txt" in str(t.get("copy", {}).get("src", ""))]
    assert len(copy) == 1
    assert copy[0]["copy"]["dest"] == "/var/data/{{ app }}-{{ env }}/data/gitea/public/robots.txt"
    assert copy[0]["when"] == "skgit.ROBOTS_TXT | default(true) | bool"


# ---------------------------------------------------------------- registration


def test_registration_is_closed_by_default():
    """Forgejo defaults to open signup; an open instance collected spam accounts."""
    svc = ini()["service"]
    assert svc["DISABLE_REGISTRATION"] == "true"
    assert svc["SHOW_REGISTRATION_BUTTON"] == "false"


@pytest.mark.parametrize("value", [False, "false"])
def test_registration_knob_reopens_signup(value):
    svc = ini(DISABLE_REGISTRATION=value)["service"]
    assert svc["DISABLE_REGISTRATION"] == "false"
    assert svc["SHOW_REGISTRATION_BUTTON"] == "true"
