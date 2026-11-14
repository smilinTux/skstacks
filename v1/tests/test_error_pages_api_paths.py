"""Machine and API traffic must never meet the error-pages middleware.

skfenceha's `default`, `default-no-crowdsec`, `upload-chain*` and `skai-chain`
chains end in `error-pages@file`: a Traefik `errors` middleware that replaces
any backend 400 and 402-599 with an HTML "Error NNN" page from a small Python
server, replaying the ORIGINAL method there (so a POST that failed upstream
reads as a 405 or 501 from the error page). API clients then never see the
app's real status or JSON error body. It hid a real skgit merge failure for
three days.

Every app with machine traffic therefore routes it through a router whose chain
is the app's own with each error-pages-bearing chain swapped for its
no-error-pages form (`<app>.API_MIDDLEWARES` replaces it), or, for apps that are
API-only, carries no error-pages at all. This file renders every app template,
resolves each router's effective chain from the REAL skfenceha middleware file,
works out which router Traefik would pick for a path, and asserts:

* the real machine paths of each app (from the app's docs and source) reach a
  router without error-pages, whichever chain the instance configures;
* a guard: a generic API-style path (`/api/...`, `/webhook/...`,
  `/.well-known/...`, `/healthz`) on ANY app host reaches a router without
  error-pages, unless the app is listed in UI_ONLY with the reason. A new app
  that routes its whole host through `default-no-crowdsec@file` fails here until
  it gets an API router (or a justified UI_ONLY entry).

The chain swap itself (CrowdSec, rate limit and headers kept, upload buffering
kept, custom middlewares kept, knob replaces) is tested per behaviour below.
"""
import pathlib
import re

import jinja2
import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
ANSIBLE = V1 / "ansible"
OPTIONAL = ANSIBLE / "optional"
RENDER_VARS = pathlib.Path(__file__).resolve().parent / "render/vars"
FENCEHA_MW = ANSIBLE / "core/skfenceha/src/config/skfenceha/dynamic-middlewares.yml.j2"

ERROR_PAGES = {"error-pages", "error-pages-all"}

# What an instance vault typically puts in a router chain list, for the apps
# whose chain comes from the vault (skbook, skstream, sksso, skhub).
INSTANCE_CHAINS = {
    "no-crowdsec": ["default-no-crowdsec@file"],
    "crowdsec": ["crowdsec-bouncer@file", "default@file"],
    "upload": ["upload-chain-no-crowdsec@file"],
}

# Facts the deploy playbooks compute and hand to the template (the render gate
# runs the real playbooks; here they are stated).
PLAYBOOK_FACTS = {
    "skflow": dict(sf_router="skflow", sf_host="skflow.render.example.test", sf_middlewares="default-no-crowdsec@file",
                   sf_sso_middleware="", sf_sso_exempt=["/api/"], sf_tls_options="none", sf_outpost_service="sksso",
                   sf_data_node="", sf_external_db=False, sf_pg_path="", sf_dump_path=""),
    "skaegis": dict(ska_router="skaegis", ska_host="skaegis.render.example.test", ska_middlewares="default@file",
                    ska_sso_middleware="", ska_tls_options="none", ska_indexer_path="", ska_backup_path="",
                    ska_certs_path="", ska_extra_ca="", ska_auth_mode="", ska_manager_path="", ska_agent_publish=""),
    "skpayment": dict(skp_router="skpayment", skp_host="skpayment.render.example.test", skp_middlewares="default@file",
                      skp_tls_options="none", skp_webhook_path="/webhook/stripe", skp_webhook_priority=1000,
                      skp_webhook_middlewares="default-no-error-pages@file", skp_webhook_ip_allowlist=False,
                      skp_data_path="", skp_data_node=""),
    "skrac": dict(skr_router="skrac", skr_host="skrac.render.example.test", skr_middlewares="default-no-crowdsec@file",
                  skr_agent_middlewares="default-no-crowdsec@file", skr_sso_middleware="", skr_tls_options="none",
                  skr_data_node="", skr_data_path="", skr_files_path="", skr_extra_ca="", skr_internal_port="4430",
                  skr_auth_mode="", skr_backup_path=""),
}
# Templates the plain-jinja harness cannot render (they need Ansible-only
# filters); none of them route an app through the shared chains.
NOT_RENDERABLE = {"skfetch", "skmon", "skwrit"}
CONTEXT_NAME = {"skorch": "skorch_cfg"}
# Apps whose router chain comes from the instance vault as a list.
VAULT_CHAIN_KEY = {"skbook": "router_middlewares", "skstream": "router_middlewares",
                   "sksso": "router_middlewares", "skhub": "router_middlewares"}


def _jinja():
    e = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    e.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return e


def example_vars(app):
    f = RENDER_VARS / f"{app}.example.yml"
    return (yaml.safe_load(f.read_text()) or {}).get(app, {}) if f.exists() else {}


FACT_KEY = re.compile(r"^(sf|ska|skp|skr)_")


def render(app, env="prod", **over):
    """Render the app's compose template. Keys like sf_*/ska_*/skp_*/skr_* override the playbook facts."""
    facts = {k: v for k, v in over.items() if FACT_KEY.match(k)}
    over = {k: v for k, v in over.items() if not FACT_KEY.match(k)}
    ctx = dict(example_vars(app), **over)
    path = OPTIONAL / app / f"src/config/{app}/{app}.yml.j2"
    return _jinja().from_string(path.read_text()).render(
        app=app, env=env, domain="example.test", cluster_name="render",
        **dict(PLAYBOOK_FACTS.get(app, {}), **facts), **{CONTEXT_NAME.get(app, app): ctx})


def apps():
    return sorted(p.name for p in OPTIONAL.iterdir() if (p / f"src/config/{p.name}/{p.name}.yml.j2").exists())


# ---------------------------------------------------------------- chains

def _chains():
    """name -> list of member names (chains), set of names that ARE error pages, from the real file."""
    text = _jinja().from_string(FENCEHA_MW.read_text()).render(
        skfenceha=example_vars("skfenceha"), env="prod", domain="example.test", cluster_name="render")
    mws = yaml.safe_load(text)["http"]["middlewares"]
    members = {n: [m.split("@")[0] for m in (d["chain"]["middlewares"])] for n, d in mws.items() if "chain" in d}
    errors = {n for n, d in mws.items() if "errors" in d}
    return members, errors


CHAIN_MEMBERS, ERROR_MWS = _chains()


def has_error_pages(middlewares):
    """True when any middleware in the comma list is, or chains to, an error-pages middleware."""
    def walk(name):
        name = name.strip().split("@")[0]
        if name in ERROR_MWS or name in ERROR_PAGES:
            return True
        return any(walk(m) for m in CHAIN_MEMBERS.get(name, []))
    return any(walk(m) for m in middlewares.split(",") if m.strip())


def test_the_real_middleware_file_defines_what_the_swap_assumes():
    assert {"error-pages", "error-pages-all"} <= ERROR_MWS
    for chain in ("default", "default-no-crowdsec", "upload-chain", "upload-chain-no-crowdsec", "skai-chain"):
        assert has_error_pages(chain), chain
    for chain in ("default-no-error-pages", "dashboard-chain"):
        assert not has_error_pages(chain), chain
    # the swap targets the templates name
    assert "rate-limit" in CHAIN_MEMBERS["default-no-error-pages"]
    assert "default-security-headers" in CHAIN_MEMBERS["default-no-error-pages"]


# ---------------------------------------------------------------- routers and rules

def routers(text):
    """name -> {label: value} for every http router label in the compose file."""
    found = {}
    for s in (yaml.safe_load(text).get("services") or {}).values():
        labels = (s.get("deploy") or {}).get("labels") or s.get("labels") or []
        if isinstance(labels, dict):
            labels = [f"{k}={v}" for k, v in labels.items()]
        for l in labels:
            m = re.match(r"traefik\.http\.routers\.([^.]+)\.(.+?)=(.*)$", str(l))
            if m:
                found.setdefault(m[1], {})[m[2]] = m[3]
    return {n: r for n, r in found.items() if "rule" in r}


def _matches(rule, host, path):
    def m(name, arg):
        if name == "Host":
            return arg.lower() == host.lower()
        if name == "Path":
            return path == arg
        if name == "PathPrefix":
            return path.startswith(arg)
        if name == "PathRegexp":
            return re.match(arg, path) is not None
        return True  # Method, Header, Query, HostRegexp: not what these rules discriminate on
    expr = re.sub(r"(\w+)\(`([^`]*)`\)", lambda x: f"_m({x[1]!r},{x[2]!r})", rule)
    expr = re.sub(r"!\s*(?=_m\(|\()", " not ", expr).replace("&&", " and ").replace("||", " or ")
    return bool(eval(expr, {"__builtins__": {}}, {"_m": m}))  # noqa: S307 - the rule text is this repo's own label


def winner(rs, host, path):
    """The router Traefik picks: highest priority, default priority = rule length."""
    cands = [(int(r.get("priority") or len(r["rule"])), n) for n, r in rs.items()
             if r.get("entrypoints", "websecure") == "websecure" and _matches(r["rule"], host, path)]
    assert cands, f"no router matches {host}{path}"
    cands.sort(reverse=True)
    return cands[0][1]


def hosts(rs):
    out = []
    for r in rs.values():
        for h in re.findall(r"Host\(`([^`]+)`\)", r["rule"]):
            if h not in out:
                out.append(h)
    return out


def main_host(rs, app):
    hs = hosts(rs)
    pref = [h for h in hs if h.startswith(app)] or hs
    return pref[0]


def chain_of(rs, name):
    return rs[name].get("middlewares", "")


# ---------------------------------------------------------------- the real machine paths

# app -> (vault override that makes the router chain realistic, paths)
MACHINE_PATHS = {
    "skorch": ["/webhook/abc", "/webhook-test/abc", "/webhook-waiting/abc", "/api/v1/workflows", "/rest/login",
               "/healthz", "/healthz/readiness", "/metrics", "/mcp/server"],
    "skport": ["/api/system/status", "/api/webhooks/0123", "/api/websocket/exec"],
    "skpdf": ["/api/v1/general/rotate-pdf", "/api/v1/info/status", "/v3/api-docs"],
    "skseek": ["/api/search", "/api/chat", "/api/config"],
    "skpeek": ["/search", "/healthz", "/autocompleter", "/config"],
    "skboard": ["/api/v1/info", "/api/v1/projects", "/dav/projects", "/.well-known/caldav", "/health"],
    "skgallery": ["/api/server/ping", "/api/assets", "/api/socket.io/", "/.well-known/immich"],
    "skflow": ["/api/version", "/api/w/main/jobs/run/p/u/f/script"],
    "skaegis": ["/api/status"],
    "skpulse": ["/api/push/token", "/api/status-page/main", "/api/badge/1/status"],
    "skrac": ["/agent.ashx", "/meshrelay.ashx", "/meshagents", "/meshsettings.ashx", "/devicefile.ashx", "/health.ashx"],
    "skpayment": ["/checkout/app", "/portal/app", "/health", "/status", "/webhook/stripe"],
    "skstor": ["/", "/bucket/key"],
    "skvector": ["/collections", "/collections/x/points", "/healthz", "/readyz", "/qdrant.Points/Upsert"],
    "skwhoami": ["/health", "/api", "/data"],
    "skbook": ["/api/books", "/api/docs", "/status"],
    "skstream": ["/library/sections", "/identity", "/:/prefs", "/hubs/home", "/status/sessions", "/media/part"],
    "sksso": ["/application/o/token/", "/application/o/userinfo/", "/application/o/jwks/",
              "/application/o/app/.well-known/openid-configuration", "/api/v3/core/users/", "/-/health/live/",
              "/.well-known/openid-configuration"],
    "skgit": ["/api/v1/repos/o/r/pulls/1/merge", "/api/v1/user", "/o/r.git/info/refs", "/o/r.git/git-upload-pack",
              "/o/r.git/git-receive-pack", "/o/r.git/info/lfs/objects/batch"],
    "skhub": ["/ocs/v2.php/apps/files_sharing/api/v1/shares", "/ocs/v1.php/cloud/capabilities",
              "/remote.php/dav/files/u/doc.txt", "/remote.php/webdav/doc.txt", "/remote.php/dav/calendars/u/x/",
              "/public.php/dav/files/token/x", "/status.php", "/.well-known/caldav", "/index.php/login/v2",
              "/index.php/apps/oauth2/api/v1/token", "/apps/notifications/api/v2/notifications"],
}


def _variants(app):
    key = VAULT_CHAIN_KEY.get(app)
    if not key:
        return [("default", {})]
    return [(n, {key: v}) for n, v in INSTANCE_CHAINS.items()]


CASES = [(app, name, over, path) for app, paths in MACHINE_PATHS.items()
         for name, over in _variants(app) for path in paths]


@pytest.mark.parametrize("app,variant,over,path", CASES, ids=[f"{a}-{v}-{p}" for a, v, _, p in CASES])
def test_machine_paths_reach_a_router_without_error_pages(app, variant, over, path):
    rs = routers(render(app, **over))
    host = main_host(rs, app)
    name = winner(rs, host, path)
    mw = chain_of(rs, name)
    assert not has_error_pages(mw), (
        f"{app}: {path} is served by router {name} with chain {mw!r}, which carries error-pages: "
        "API clients would get the HTML error page instead of the app's own status")


# ---------------------------------------------------------------- guard

# Apps whose error-pages-bearing routers serve only a human UI, with the reason.
# An app with ANY router that carries error-pages must be in MACHINE_PATHS (its
# real machine paths are asserted above) or here. Empty on purpose: every app
# that uses the shared chains today has an API.
UI_ONLY = {}
# A router whose rule names a path like these is declaring machine traffic.
API_WORDS = re.compile(r"api|webhook|\.well-known|/dav|/ocs|remote\.php|public\.php|status\.php|health|metrics|"
                       r"/v2|oauth|/application/o|\.ashx|/token|/library|/identity|info/refs|git-")


def _renderable():
    return [a for a in apps() if a not in NOT_RENDERABLE]


def _guard_cases():
    return [(app, name, over) for app in _renderable() for name, over in _variants(app)]


@pytest.mark.parametrize("app,variant,over", _guard_cases(), ids=[f"{a}-{n}" for a, n, _ in _guard_cases()])
def test_a_router_that_names_an_api_path_never_carries_error_pages(app, variant, over):
    bad = []
    for name, r in routers(render(app, **over)).items():
        paths = " ".join(re.findall(r"Path(?:Prefix|Regexp)?\(`([^`]*)`\)", r["rule"]))
        if API_WORDS.search(paths) and has_error_pages(r.get("middlewares", "")):
            bad.append(f"router {name}: rule {r['rule']!r}, chain {r.get('middlewares')!r}")
    assert not bad, f"{app}: a router for machine paths carries error-pages (HTML replaces the real status):\n  " + "\n  ".join(bad)


@pytest.mark.parametrize("app,variant,over", _guard_cases(), ids=[f"{a}-{n}" for a, n, _ in _guard_cases()])
def test_an_app_with_an_error_pages_router_has_its_machine_paths_covered_or_is_ui_only(app, variant, over):
    """A new app routed through default-no-crowdsec@file lands here until it is classified.

    Add its API router (see skorch or skport for the pattern) and list its real
    machine paths in MACHINE_PATHS, or, when it truly serves only a browser,
    add it to UI_ONLY with the reason.
    """
    rs = routers(render(app, **over))
    with_ep = [n for n, r in rs.items() if has_error_pages(r.get("middlewares", ""))]
    if not with_ep:
        return
    assert app in MACHINE_PATHS or app in UI_ONLY, (
        f"{app}: router(s) {with_ep} carry error-pages, which replaces API errors with an HTML page. "
        "Give the app an API router and list its machine paths in MACHINE_PATHS, or add it to UI_ONLY with a reason.")


def test_every_app_template_renders_in_the_harness_or_is_listed():
    """A template the harness cannot render would silently escape the guard."""
    failed = []
    for app in _renderable():
        try:
            yaml.safe_load(render(app))
        except Exception as ex:  # noqa: BLE001
            failed.append(f"{app}: {type(ex).__name__}: {str(ex)[:80]}")
    assert not failed, ("Give the harness what these templates need (PLAYBOOK_FACTS or example vars) or list "
                        "them in NOT_RENDERABLE with a reason:\n  " + "\n  ".join(failed))


def test_ui_only_entries_are_not_stale():
    for app in UI_ONLY:
        assert app in apps(), app
        assert app not in MACHINE_PATHS, app


# ---------------------------------------------------------------- the derived chain

def _api_chain(app, **over):
    rs = routers(render(app, **over))
    names = [n for n in rs if n.endswith("-api")]
    assert len(names) == 1, (app, names)
    return rs[names[0]]["middlewares"]


@pytest.mark.parametrize("app,knob", [
    ("skorch", "TRAEFIK_MIDDLEWARES"), ("skport", "TRAEFIK_MIDDLEWARES"), ("skpdf", "TRAEFIK_MIDDLEWARES"),
    ("skseek", "TRAEFIK_MIDDLEWARES"), ("skpeek", "TRAEFIK_MIDDLEWARES"), ("skboard", "TRAEFIK_MIDDLEWARES"),
    ("skgallery", "TRAEFIK_MIDDLEWARES")])
def test_derived_chain_keeps_crowdsec_and_custom_middlewares_and_drops_only_error_pages(app, knob):
    got = _api_chain(app, **{knob: "crowdsec-bouncer@file,my-mw@file,default@file"})
    assert got == "crowdsec-bouncer@file,my-mw@file,default-no-error-pages@file"
    got = _api_chain(app, **{knob: "default-no-crowdsec@file"})
    assert got == "default-no-error-pages@file"
    # a chain that has both default forms must not name the swap target twice
    got = _api_chain(app, **{knob: "default@file,default-no-crowdsec@file"})
    assert got == "default-no-error-pages@file"


def test_upload_chains_keep_their_buffering_and_headers_but_lose_error_pages():
    # skgallery: photos and videos up to 20 GB go through the upload chain
    assert _api_chain("skgallery") == "large-upload-buffering@file,default-security-headers@file"
    assert _api_chain("skgallery", TRAEFIK_MIDDLEWARES="upload-chain@file") == \
        "large-upload-buffering@file,default-security-headers@file"
    assert not has_error_pages(_api_chain("skgallery"))


def test_skpdf_keeps_its_own_upload_buffering_middleware():
    assert _api_chain("skpdf") == "default-no-error-pages@file,skpdf-upload"
    rs = routers(render("skpdf", "dev"))
    assert rs["skpdf-dev-api"]["middlewares"] == "default-no-error-pages@file,skpdf-dev-upload"


@pytest.mark.parametrize("app", ["skorch", "skport", "skpdf", "skseek", "skpeek", "skboard", "skgallery", "skflow",
                                 "skaegis", "skpayment", "skbook", "skstream", "sksso", "skhub"])
def test_api_middlewares_knob_replaces_the_derived_chain(app):
    over = {"API_MIDDLEWARES": "ipwhitelist@file,default-no-error-pages@file"}
    key = VAULT_CHAIN_KEY.get(app)
    if key:
        over[key] = INSTANCE_CHAINS["no-crowdsec"]
    rs = routers(render(app, **over))
    api = [r for n, r in rs.items() if n.endswith("-api")]
    assert len(api) == 1 and api[0]["middlewares"].split(",")[0] == "ipwhitelist@file", (app, api)
    # an empty knob is the same as unset
    over["API_MIDDLEWARES"] = ""
    assert render(app, **over) == render(app, **{k: v for k, v in over.items() if k != "API_MIDDLEWARES"})


@pytest.mark.parametrize("app", ["skorch", "skport", "skpdf", "skseek", "skpeek", "skboard", "skgallery", "skflow",
                                 "skaegis", "skpayment", "skbook", "skstream", "sksso", "skhub"])
def test_api_router_is_a_sibling_of_the_main_router(app):
    """Same host, entrypoint, certresolver and service as the main router; priority above it."""
    over = {VAULT_CHAIN_KEY[app]: INSTANCE_CHAINS["no-crowdsec"]} if app in VAULT_CHAIN_KEY else {}
    rs = routers(render(app, **over))
    api_name = next(n for n in rs if n.endswith("-api"))
    api = rs[api_name]
    main = rs[api_name[: -len("-api")]] if api_name[: -len("-api")] in rs else rs[api_name.replace("-api", "-secure")]
    assert api["entrypoints"] == main["entrypoints"] == "websecure"
    assert api["tls.certresolver"] == main["tls.certresolver"]
    assert api["service"] == main["service"]
    assert int(api["priority"]) > int(main.get("priority") or len(main["rule"])), app
    # `docker stack deploy` interpolates `$` in a label: the path part must have none (sksso's Host() is
    # `${BASE_DOMAIN}` on purpose, in the main router as well)
    assert "$" not in api["rule"].split("&&", 1)[1]


def test_vault_chain_apps_render_no_api_router_when_the_instance_sets_no_chain():
    """No router_middlewares, no error pages to bypass: nothing to add."""
    for app in ("skbook", "skstream", "sksso"):
        assert not [n for n in routers(render(app)) if n.endswith("-api")], app


def test_skflow_api_router_exists_with_and_without_sso_and_never_carries_forward_auth():
    rs = routers(render("skflow"))
    assert rs["skflow-api"]["rule"].endswith("(PathPrefix(`/api/`))")
    assert rs["skflow-api"]["middlewares"] == "default-no-error-pages@file"
    rs = routers(render("skflow", sf_sso_middleware="authentik@file", sf_middlewares="crowdsec-bouncer@file,default@file"))
    assert rs["skflow-api"]["middlewares"] == "crowdsec-bouncer@file,default-no-error-pages@file"
    assert "authentik" not in rs["skflow-api"]["middlewares"]
    # SSO on but no exempt prefixes: every path stays behind forward auth, no API router
    rs = routers(render("skflow", sf_sso_middleware="authentik@file", sf_sso_exempt=[]))
    assert "skflow-api" not in rs


def test_skaegis_api_router_keeps_the_sso_middleware():
    rs = routers(render("skaegis", ska_sso_middleware="authentik@file"))
    assert rs["skaegis-api"]["middlewares"] == "default-no-error-pages@file,authentik@file"
    assert rs["skaegis"]["middlewares"] == "default@file,authentik@file"


def test_skpulse_machine_routers_default_to_the_chain_without_error_pages_and_the_knobs_still_win():
    rs = routers(render("skpulse"))
    assert rs["skpulse-public"]["middlewares"] == "default-no-error-pages@file"
    assert has_error_pages(rs["skpulse"]["middlewares"].split(",authentik")[0])  # the dashboard keeps its error pages
    rs = routers(render("skpulse", BYPASS_TRAEFIK_MIDDLEWARES="default-no-crowdsec@file"))
    assert rs["skpulse-public"]["middlewares"] == "default-no-crowdsec@file"
    rs = routers(render("skpulse", METRICS_BYPASS_SSO=True))
    assert rs["skpulse-metrics"]["middlewares"] == "default-no-error-pages@file"


def test_skrac_agent_router_defaults_to_the_chain_without_error_pages_and_the_knob_wins():
    text = render("skrac")
    assert routers(text)["skrac-agent"]["middlewares"] == "default-no-error-pages@file"
    assert has_error_pages(routers(text)["skrac"]["middlewares"])
    text = render("skrac", AGENT_TRAEFIK_MIDDLEWARES="default-no-crowdsec@file")
    assert routers(text)["skrac-agent"]["middlewares"] == "default-no-crowdsec@file"


def test_skpayment_webhook_default_has_no_error_pages_so_stripe_sees_the_real_status():
    rs = routers(render("skpayment"))
    assert not has_error_pages(rs["skpayment-webhook"]["middlewares"])
    assert not has_error_pages(rs["skpayment-api"]["middlewares"])


def test_api_only_apps_default_to_a_chain_without_error_pages():
    for app, name in (("skstor", "skstor"), ("skvector", "skvector"), ("skvector", "skvector-grpc")):
        assert not has_error_pages(routers(render(app))[name]["middlewares"]), (app, name)
    for n in ("skwhoami01", "skwhoami02", "skwhoami03"):
        assert not has_error_pages(routers(render("skwhoami"))[n]["middlewares"]), n
    # an explicit knob still wins verbatim
    assert routers(render("skstor", TRAEFIK_MIDDLEWARES="default@file"))["skstor"]["middlewares"] == "default@file"


def test_skhub_api_router_keeps_the_nextcloud_dav_redirects_and_the_instance_chain():
    rs = routers(render("skhub", router_middlewares=["default-no-crowdsec@file"]))
    assert rs["skhub-api"]["middlewares"] == "skhub,skhub-dav,default-no-error-pages@file"
    assert rs["skhub-secure"]["middlewares"] == "skhub,skhub-dav,default-no-crowdsec@file"
    rs = routers(render("skhub", router_middlewares=["crowdsec-bouncer@file", "default@file"],
                        router_middlewares_pre=["my-pre@file"]))
    assert rs["skhub-api"]["middlewares"] == "my-pre@file,skhub,skhub-dav,crowdsec-bouncer@file,default-no-error-pages@file"


def test_skstream_plex_ui_keeps_error_pages_everything_else_does_not():
    rs = routers(render("skstream", router_middlewares=["default-no-crowdsec@file"]))
    host = main_host(rs, "skstream")
    assert has_error_pages(chain_of(rs, winner(rs, host, "/web/index.html")))
    assert not has_error_pages(chain_of(rs, winner(rs, host, "/library/sections")))
