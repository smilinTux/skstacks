"""sksec's CrowdSec bouncer as a Traefik LOCAL plugin (v2.26.38).

The old bouncer was a third-party forwardAuth container (no upstream release
since 2022, unpatched glibc). It is replaced by the vendored
maxlerebourg/crowdsec-bouncer-traefik-plugin, which skfence/skfenceha load
from /plugins-local (experimental.localPlugins), so Traefik downloads
nothing at start. sksec.BOUNCER_MODE picks plugin (default) or forwardauth
(the old container, kept for one release). Both modes define the SAME
middleware name, crowdsec-bouncer@file, so no router or chain changes.

What is checked here:
- the vendored tree matches SHA256SUMS, carries its license, and is the
  version the deploy task pins;
- sksec renders the right thing per mode (compose + dynamic middleware),
  the trust lists map onto the plugin's, and the key never lands inline;
- skfence and skfenceha (rendered through REAL Ansible) declare the plugin
  in every Traefik static config, mount the installed bundle read-only at
  /plugins-local, and drop both when CROWDSEC_PLUGIN_ENABLED is false;
- no router or chain references a middleware that is not defined in that
  Traefik's file-provider namespace, in either mode.
"""
import hashlib
import pathlib
import re
import shutil
import sys

import jinja2
import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

V1 = pathlib.Path(__file__).resolve().parents[1]
ANSIBLE = V1 / "ansible"
VENDOR = ANSIBLE / "shared/third_party/crowdsec-bouncer-traefik-plugin"
TASK = ANSIBLE / "shared/tasks/traefik_crowdsec_plugin.yml"
SKSEC = ANSIBLE / "core/sksec"
DYNAMIC = SKSEC / "src/config/sksec/sksec-dynamic.yml.j2"
COMPOSE = SKSEC / "src/config/sksec/sksec.yml.j2"
MODULE = "github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin"
PLUGIN = "crowdsec-bouncer-traefik-plugin"  # experimental.localPlugins key
KEY = "COMPUTED-KEY-SENTINEL"

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


def pinned_version():
    m = re.search(r"traefik_crowdsec_plugin_version:\s*(v[0-9][\w.-]*)", TASK.read_text())
    assert m, "traefik_crowdsec_plugin_version not found in the shared task"
    return m.group(1)


# ---------------------------------------------------------------- vendored tree

def test_vendored_tree_matches_sha256sums():
    lines = (VENDOR / "SHA256SUMS").read_text().splitlines()
    listed = {}
    for line in lines:
        digest, path = line.split("  ", 1)
        listed[path] = digest
    shipped = {str(p.relative_to(VENDOR)) for p in (VENDOR / "src").rglob("*") if p.is_file()}
    assert shipped == set(listed), "files added to or missing from the vendored tree (rerun the vendor tool)"
    for path, digest in listed.items():
        assert hashlib.sha256((VENDOR / path).read_bytes()).hexdigest() == digest, path


def test_vendored_tree_is_the_pinned_version_and_module():
    v = pinned_version()
    src = VENDOR / "src"
    assert f'pluginVersion = "{v}"' in (src / "version.go").read_text()
    assert (src / "go.mod").read_text().splitlines()[0] == f"module {MODULE}"
    manifest = yaml.safe_load((src / ".traefik.yml").read_text())
    assert manifest["import"] == MODULE and manifest["type"] == "middleware"
    assert v in (VENDOR / "NOTICE.md").read_text()


def test_vendored_manifest_carries_no_example_key():
    # Secret scanners walk every ref; upstream's testData example key is blanked.
    manifest = yaml.safe_load((VENDOR / "src" / ".traefik.yml").read_text())
    assert manifest["testData"].get("CrowdsecLapiKey", "") == ""


def test_vendored_tree_keeps_its_licenses_and_no_tests():
    src = VENDOR / "src"
    assert "Apache License" in (src / "LICENSE").read_text()
    assert (src / "vendor/modules.txt").is_file()
    for lic in ("vendor/github.com/leprosus/golang-ttl-map/License.md",
                "vendor/github.com/maxlerebourg/simpleredis/LICENSE"):
        assert (src / lic).is_file(), lic
    assert not list(src.rglob("*_test.go"))


# ---------------------------------------------------------------- sksec, plain jinja

BASE = {"APP_ENV": "prod", "LOG_MAX_SIZE": "10m", "LOG_MAX_FILES": "3", "CROWDSEC_CPU_LIMIT": "0.5",
        "CROWDSEC_CPU_RESERVATION": "0.1", "BOUNCER_CPU_LIMIT": "0.25", "BOUNCER_CPU_RESERVATION": "0.05"}


def render(path, **sksec):
    j = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    j.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "on", "1")
    return j.from_string(path.read_text()).render(
        env="prod", app="sksec", sksec=dict(BASE, **sksec), traefik_service_name="skfenceha",
        computed_bouncer_key=KEY)


def middleware(**sksec):
    text = render(DYNAMIC, **sksec)
    assert KEY not in text, "the bouncer key must never be rendered into Traefik's dynamic config"
    return yaml.safe_load(text)["http"]["middlewares"]["crowdsec-bouncer"]


def test_plugin_is_the_default_mode():
    mw = middleware()
    assert set(mw) == {"plugin"} and set(mw["plugin"]) == {PLUGIN}
    cfg = mw["plugin"][PLUGIN]
    assert cfg["enabled"] is True
    assert cfg["crowdsecMode"] == "stream"
    assert cfg["crowdsecLapiScheme"] == "http"
    assert cfg["crowdsecLapiHost"] == "sksec-prod_crowdsec:8080"
    assert cfg["crowdsecLapiKeyFile"] == "/etc/traefik/dynamic/sksec-lapi.key"
    assert "crowdsecLapiKey" not in cfg
    assert cfg["updateIntervalSeconds"] == 60 and cfg["updateMaxFailure"] == -1
    assert cfg["httpTimeoutSeconds"] == 10 and cfg["streamStartupBlock"] is True
    assert cfg["crowdsecAppsecEnabled"] is False
    assert "redisCacheEnabled" not in cfg  # in-memory cache
    assert "forwardedHeadersTrustedIPs" not in cfg and "clientTrustedIPs" not in cfg
    compose = yaml.safe_load(render(COMPOSE))
    assert "bouncer-traefik" not in compose["services"]
    assert "depends_on" not in compose["services"]["crowdsec"]


def test_plugin_knobs_map_onto_the_plugin_config():
    cfg = middleware(BOUNCER_TRUSTED_PROXIES=["172.16.200.0/24", "2001:db8::/32"],
                     BOUNCER_CLIENT_TRUSTED_IPS=["192.0.2.7"], BOUNCER_UPDATE_INTERVAL_SECONDS=30,
                     BOUNCER_UPDATE_MAX_FAILURE=-1, CROWDSEC_TIMEOUT=4, BOUNCER_LOG_LEVEL="debug",
                     CROWDSEC_AGENT_HOST="lapi.example.test:8081")["plugin"][PLUGIN]
    # the forwardauth bouncer's TRUSTED_PROXIES, same list, same right-to-left walk
    assert cfg["forwardedHeadersTrustedIPs"] == ["172.16.200.0/24", "2001:db8::/32"]
    assert cfg["clientTrustedIPs"] == ["192.0.2.7"]
    assert cfg["updateIntervalSeconds"] == 30 and cfg["updateMaxFailure"] == -1
    assert cfg["httpTimeoutSeconds"] == 4 and cfg["logLevel"] == "DEBUG"
    assert cfg["crowdsecLapiHost"] == "lapi.example.test:8081"


def test_forwardauth_mode_keeps_the_old_container():
    mw = middleware(BOUNCER_MODE="forwardauth")
    assert mw["forwardAuth"]["address"] == "http://sksec-prod_bouncer-traefik:8080/api/v1/forwardAuth"
    compose = yaml.safe_load(render(COMPOSE, BOUNCER_MODE="forwardauth"))
    assert "bouncer-traefik" in compose["services"]
    assert compose["services"]["crowdsec"]["depends_on"] == ["bouncer-traefik"]


def test_both_modes_define_the_name_every_reference_uses():
    """Routers and chains across the framework name crowdsec-bouncer@file
    literally; both modes must define exactly that middleware."""
    names = set()
    for f in ANSIBLE.rglob("*"):
        if f.is_file() and f.suffix in (".j2", ".yml", ".yaml") and "third_party" not in f.parts:
            names |= set(re.findall(r"(?<![\w-])(crowdsec[\w-]*)@file\b", f.read_text(errors="ignore")))
    assert names == {"crowdsec-bouncer"}, names
    for mode in ("plugin", "forwardauth"):
        assert set(yaml.safe_load(render(DYNAMIC, BOUNCER_MODE=mode))["http"]["middlewares"]) == {"crowdsec-bouncer"}


# ---------------------------------------------------------------- real Ansible renders

def run(tmp: pathlib.Path, svc: str, vault: dict, env: str = "prod") -> pathlib.Path:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / f"vars-{svc}"
    if vars_dir.exists():
        shutil.rmtree(vars_dir)
    vars_dir.mkdir()
    for f in rp.VARS.glob(f"{svc}.with.*.yml"):
        shutil.copy(f, vars_dir / f.name)
    (vars_dir / f"{svc}.example.yml").write_text(yaml.safe_dump({svc: vault}))
    run_dir = work / f"{svc}-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "core" / svc / f"deploy_{svc}-{env}.yml"
        res = rp.run_one(work, "core", svc, env, pb, "base")
    finally:
        rp.VARS = saved
    log = (run_dir / "ansible.log").read_text() if (run_dir / "ansible.log").exists() else ""
    assert res["status"] == "PASS", (res["findings"], log[-3000:])
    return run_dir / "out"


def vault_of(svc, pas="set"):
    v = rp.load_vars(rp.VARS / f"{svc}.example.yml")[svc]
    return rp.deep_merge(v, rp.load_vars(rp.VARS / f"{svc}.set.yml")[svc]) if pas == "set" else v


TRAEFIK_ROLES = {
    # svc: {compose service: static config file it mounts}
    "skfence": {"traefik": "traefik.yml"},
    "skfenceha": {"traefik-acme": "traefik.yml", "traefik-worker": "traefik-worker.yml"},
}


@pytest.fixture(scope="module")
def traefik_renders(tmp_path_factory):
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook not installed")
    tmp = tmp_path_factory.mktemp("traefik")
    out = {}
    for svc in TRAEFIK_ROLES:
        rendered = run(tmp, svc, vault_of(svc))
        keep = tmp / f"{svc}-on"
        shutil.copytree(rendered, keep)
        out[svc] = keep
    sksec_out = {}
    for mode in ("plugin", "forwardauth"):
        rendered = run(tmp, "sksec", vault_of("sksec") | {"BOUNCER_MODE": mode})
        keep = tmp / f"sksec-{mode}"
        shutil.copytree(rendered, keep)
        sksec_out[mode] = keep
    out["sksec"] = sksec_out
    return out


@needs_ansible
@pytest.mark.parametrize("svc", sorted(TRAEFIK_ROLES))
def test_every_traefik_static_config_declares_the_local_plugin(traefik_renders, svc):
    cfg = traefik_renders[svc] / f"var/data/config/{svc}-prod"
    for static in TRAEFIK_ROLES[svc].values():
        doc = yaml.safe_load((cfg / static).read_text())
        assert doc["experimental"] == {"localPlugins": {PLUGIN: {"moduleName": MODULE}}}, static
        assert "plugins" not in doc["experimental"], "no remote plugin download"


@needs_ansible
@pytest.mark.parametrize("svc", sorted(TRAEFIK_ROLES))
def test_every_traefik_service_mounts_the_installed_bundle(traefik_renders, svc):
    out = traefik_renders[svc]
    cfg = f"/var/data/config/{svc}-prod"
    bundle = f"{cfg}/plugins-local/{PLUGIN}-{pinned_version()}"
    compose = yaml.safe_load((out / f"var/data/config/{svc}-prod/{svc}.yml").read_text())
    for name in TRAEFIK_ROLES[svc]:
        assert f"{bundle}:/plugins-local:ro" in compose["services"][name]["volumes"], name
    # the shared task copies the vendored source to exactly the mounted path,
    # laid out as Traefik expects: <mount>/src/<moduleName>/.traefik.yml
    installed = out / "_copy" / bundle.lstrip("/") / "src" / MODULE
    assert (installed / ".traefik.yml").is_file() and (installed / "bouncer.go").is_file()
    assert (out / "_copy" / bundle.lstrip("/") / "SHA256SUMS").is_file()


@needs_ansible
@pytest.mark.parametrize("svc", sorted(TRAEFIK_ROLES))
def test_plugin_can_be_switched_off_per_traefik_role(tmp_path, svc):
    out = run(tmp_path, svc, vault_of(svc) | {"CROWDSEC_PLUGIN_ENABLED": False})
    cfg = out / f"var/data/config/{svc}-prod"
    for static in TRAEFIK_ROLES[svc].values():
        assert "experimental" not in yaml.safe_load((cfg / static).read_text())
    assert "/plugins-local" not in (cfg / f"{svc}.yml").read_text()
    assert not list((out / "_copy").rglob(".traefik.yml")) if (out / "_copy").exists() else True


@needs_ansible
def test_sksec_writes_the_key_file_only_in_plugin_mode(traefik_renders):
    for d in ("dynamic", "dynamic_acme"):
        key = traefik_renders["sksec"]["plugin"] / f"_copy/var/data/config/skfenceha-prod/{d}/sksec-lapi.key"
        assert key.read_text() == "example-crowdsec-bouncer-api-key-not-a-secret"
        assert not (traefik_renders["sksec"]["forwardauth"] / f"_copy/var/data/config/skfenceha-prod/{d}/sksec-lapi.key").exists()


def defined_and_referenced(dynamic_dir: pathlib.Path, extra_docs, compose_text: str):
    defined, refs = set(), set()
    docs = [yaml.safe_load(f.read_text()) or {} for f in sorted(dynamic_dir.glob("*.yml"))] + list(extra_docs)
    for doc in docs:
        http = doc.get("http") or {}
        for name, mw in (http.get("middlewares") or {}).items():
            defined.add(name)
            refs.update(((mw or {}).get("chain") or {}).get("middlewares") or [])
        for router in (http.get("routers") or {}).values():
            refs.update(router.get("middlewares") or [])
    for value in re.findall(r"\.middlewares=([^\"'\s]+)", compose_text):
        refs.update(value.split(","))
    return defined, refs


@needs_ansible
@pytest.mark.parametrize("mode", ["plugin", "forwardauth"])
@pytest.mark.parametrize("svc", sorted(TRAEFIK_ROLES))
def test_no_router_references_a_missing_middleware(traefik_renders, svc, mode):
    """Each Traefik role has its own file-provider namespace (skfenceha: the
    workers read dynamic/, the ACME master dynamic_acme/). With CROWDSEC_ENABLED
    (the set pass) the default chain names crowdsec-bouncer@file, which only
    sksec's file defines: every @file (or bare) reference must resolve."""
    cfg = traefik_renders[svc] / f"var/data/config/{svc}-prod"
    compose = (cfg / f"{svc}.yml").read_text()
    sksec_cfg = traefik_renders["sksec"][mode] / "var/data/config/skfenceha-prod"
    dirs = ["dynamic"] + (["dynamic_acme"] if svc == "skfenceha" else [])
    for d in dirs:
        sksec_doc = yaml.safe_load((sksec_cfg / d / "sksec.yml").read_text())
        defined, refs = defined_and_referenced(cfg / d, [sksec_doc], compose)
        assert "crowdsec-bouncer@file" in refs, "the set pass enables CrowdSec on the default chain"
        wanted = {r[: -len("@file")] for r in refs if r.endswith("@file")} | {r for r in refs if "@" not in r}
        assert wanted <= defined, (d, sorted(wanted - defined))
        if mode == "plugin":
            plugin_names = {n for mw in sksec_doc["http"]["middlewares"].values() for n in (mw.get("plugin") or {})}
            static = yaml.safe_load((cfg / ("traefik-worker.yml" if d == "dynamic" and svc == "skfenceha" else "traefik.yml")).read_text())
            assert plugin_names <= set(static["experimental"]["localPlugins"]), d


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_sksec_play_orders_the_switch_safely(env):
    """Order is what keeps both switches free of 403s: plugin mode checks
    Traefik first, writes key + middleware, rolls skfenceha's Traefik, and
    only then deploys (prunes) the stack; forwardauth deploys the stack,
    waits for the bouncer, and only then points the middleware at it."""
    play = yaml.safe_load((SKSEC / f"deploy_sksec-{env}.yml").read_text())[-1]
    names = [t.get("name", "") for t in play["tasks"]]

    def at(prefix):
        hits = [i for i, n in enumerate(names) if n.startswith(prefix)]
        assert len(hits) == 1, (prefix, hits)
        return hits[0]

    order = [at("Check the running Traefik loads the CrowdSec bouncer plugin"),
             at("Stop when Traefik is not ready"),
             at("Write the CrowdSec bouncer key for the Traefik plugin"),
             at("Deploy CrowdSec bouncer middleware configuration (plugin mode"),
             at("Roll Traefik so every node loads the plugin middleware"),
             at("Deploy {{ app }} stack"),
             at("Wait for the forwardAuth bouncer to run"),
             at("Deploy CrowdSec bouncer middleware configuration (forwardauth mode"),
             at("Remove the plugin's bouncer key file")]
    assert order == sorted(order), [names[i] for i in order]
    roll = play["tasks"][at("Roll Traefik so every node loads the plugin middleware")]
    assert "sksec_plugin_dynamic is changed" in roll["when"]
    assert any("skfenceha" in w for w in roll["when"])
    key = play["tasks"][at("Write the CrowdSec bouncer key for the Traefik plugin")]
    assert key["no_log"] is True and key["ansible.builtin.copy"]["mode"] == "0600"
