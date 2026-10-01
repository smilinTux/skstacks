"""skfetch.SLSKD_ENABLED: slskd (Soulseek) plus Soularr (Lidarr to slskd bridge).

A production instance added both by hand to the host-level compose: slskd shares gluetun's
network namespace (the same VPN tunnel as qBittorrent), Soularr reads Lidarr's wanted list and
asks slskd for each album, and downloads land on the media dataset where Lidarr imports them.
Without the knob the next skfetch deploy rewrote the compose file without them (and the
systemd unit restarted the project, so the containers would stop). Contract held here:
  * off by default; off renders compose, env file and playbook output exactly as before
  * on: both services digest-pinned, log caps, restart on-failure, mem_limit, uid 1000
  * slskd runs in gluetun's namespace and waits for it to be healthy; Soularr on the bridge
  * secrets only in 0600 files (skfetch.env, slskd.yml, soularr config.ini), never in compose
  * slskd.yml and config.ini are seeds: written only if absent (FORCE_SEED)
  * the five slskd secrets are required when enabled
"""
import configparser
import json
import pathlib

import jinja2
import pytest
import yaml

APP = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skfetch"
SRC = APP / "src/config/skfetch"
COMPOSE = SRC / "skfetch.yml.j2"
ENVFILE = SRC / "skfetch.env.j2"
SLSKD_YML = SRC / "slskd.yml.j2"
SOULARR_INI = SRC / "soularr-config.ini.j2"
README = APP / "README.md"
PLAYBOOKS = {e: APP / f"deploy_skfetch-{e}.yml" for e in ("dev", "staging", "prod")}
BASE = {
    "CLUSTERNAME": "demo", "DOMAIN": "example.test", "MEDIA_NODE": "node-a", "LAN_BIND_ADDRESS": "192.0.2.10",
    "OPENVPN_USER": "vpnuserexample", "OPENVPN_PASSWORD": "vpnpassexample",
    "SONARR_API_KEY": "sonarrkeyexample", "RADARR_API_KEY": "radarrkeyexample",
    "LIDARR_API_KEY": "lidarrkeyexample", "PROWLARR_API_KEY": "prowlarrkeyexample",
}
SLSKD = {
    "SLSKD_ENABLED": True,
    "SLSKD_SOULSEEK_USERNAME": "soulseekuserexample", "SLSKD_SOULSEEK_PASSWORD": "soulseekpassexample",
    "SLSKD_WEB_USERNAME": "webuserexample", "SLSKD_WEB_PASSWORD": "webpassexample",
    "SLSKD_API_KEY": "slskdapikeyexample0000000000000000",
}
SECRET_VALUES = [v for k, v in {**BASE, **SLSKD}.items() if k not in ("CLUSTERNAME", "DOMAIN", "MEDIA_NODE",
                                                                     "LAN_BIND_ADDRESS", "SLSKD_ENABLED")]


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    env.filters["to_json"] = json.dumps
    return env


def render(path, env="prod", **over):
    return _env().from_string(path.read_text()).render(app="skfetch", env=env, skfetch=dict(BASE, **over))


def compose(env="prod", **over):
    return yaml.safe_load(render(COMPOSE, env, **over))


# ---- off by default -------------------------------------------------------------

@pytest.mark.parametrize("flag", [None, False, "false", "no"])
def test_off_renders_exactly_as_before(flag):
    over = {} if flag is None else {"SLSKD_ENABLED": flag}
    assert render(COMPOSE, **over) == render(COMPOSE)
    assert "slskd" not in render(COMPOSE, **over) and "soularr" not in render(COMPOSE, **over)
    assert "SLSKD" not in render(ENVFILE, **over)


# ---- compose when on ------------------------------------------------------------

def test_on_adds_both_services_pinned_and_capped():
    svcs = compose(**SLSKD)["services"]
    for name in ("slskd", "soularr"):
        s = svcs[name]
        assert "@sha256:" in s["image"], name
        assert s["restart"] == "on-failure" and s["mem_limit"], name
        assert s["logging"]["options"] == {"max-size": "10m", "max-file": "3"}, name
        assert s["user"] == "1000:1000", name


def test_slskd_shares_the_vpn_namespace_and_waits_for_it():
    s = compose(**SLSKD)["services"]["slskd"]
    assert s["network_mode"] == "service:gluetun"
    assert s["depends_on"] == {"gluetun": {"condition": "service_healthy"}}
    assert "networks" not in s and "ports" not in s
    assert s["environment"]["SLSKD_REMOTE_CONFIGURATION"] == "false"


def test_slskd_state_local_downloads_on_the_dataset():
    s = compose(**SLSKD)["services"]["slskd"]
    assert s["volumes"] == ["/var/lib/skfetch/slskd:/app", "/var/data/skfetch-prod/slskd-downloads:/app/downloads"]


def test_soularr_on_the_bridge_after_lidarr_and_slskd():
    s = compose(**SLSKD)["services"]["soularr"]
    assert s["networks"] == {"skfetch": {"ipv4_address": "172.30.60.21"}}
    assert s["depends_on"] == ["lidarr", "slskd"]
    assert s["volumes"] == ["/var/lib/skfetch/soularr:/data", "/var/data/skfetch-prod/slskd-downloads:/downloads"]
    assert s["environment"]["SCRIPT_INTERVAL"] == "300" and s["environment"]["WEBUI_ENABLED"] == "false"


def test_paths_and_bridge_follow_the_knobs():
    svcs = compose("dev", **SLSKD, MEDIA_PATH="/srv/media", BRIDGE_SUBNET="10.9.8.0/24")["services"]
    assert svcs["slskd"]["volumes"] == ["/var/lib/skfetch-dev/slskd:/app", "/srv/media/slskd-downloads:/app/downloads"]
    assert svcs["soularr"]["networks"]["skfetch"]["ipv4_address"] == "10.9.8.21"


def test_tunables_are_knobs():
    svcs = compose(**SLSKD, SLSKD_MEM_LIMIT="1g", SOULARR_MEM_LIMIT="128m", SOULARR_INTERVAL=600,
                   SLSKD_IMAGE="slskd/slskd@sha256:" + "c" * 64)["services"]
    assert svcs["slskd"]["mem_limit"] == "1g" and svcs["soularr"]["mem_limit"] == "128m"
    assert svcs["soularr"]["environment"]["SCRIPT_INTERVAL"] == "600"
    assert svcs["slskd"]["image"] == "slskd/slskd@sha256:" + "c" * 64


def test_on_leaves_the_other_services_alone():
    a, b = compose()["services"], compose(**SLSKD)["services"]
    for name in a:
        assert a[name] == b[name], name


def test_no_secret_value_in_the_compose_text():
    text = render(COMPOSE, **SLSKD)
    for v in SECRET_VALUES:
        assert v not in text


# ---- secrets files --------------------------------------------------------------

def test_env_file_carries_the_slskd_secrets_when_on():
    lines = render(ENVFILE, **SLSKD).splitlines()
    for k in ("SLSKD_SOULSEEK_USERNAME", "SLSKD_SOULSEEK_PASSWORD", "SLSKD_WEB_USERNAME",
              "SLSKD_WEB_PASSWORD", "SLSKD_API_KEY"):
        assert f"{k}={SLSKD[k]}" in lines, k


def test_slskd_yml_seed():
    y = yaml.safe_load(render(SLSKD_YML, **SLSKD))
    assert y["soulseek"] == {"username": SLSKD["SLSKD_SOULSEEK_USERNAME"],
                             "password": SLSKD["SLSKD_SOULSEEK_PASSWORD"], "listen_port": 50300}
    auth = y["web"]["authentication"]
    assert y["web"]["port"] == 5030
    assert auth["username"] == SLSKD["SLSKD_WEB_USERNAME"] and auth["password"] == SLSKD["SLSKD_WEB_PASSWORD"]
    assert auth["api_keys"]["soularr"]["key"] == SLSKD["SLSKD_API_KEY"]
    assert y["directories"] == {"downloads": "/app/downloads", "incomplete": "/app/incomplete"}
    assert y["shares"] == {"directories": []}


def test_slskd_yml_quotes_awkward_values():
    y = yaml.safe_load(render(SLSKD_YML, **dict(SLSKD, SLSKD_WEB_PASSWORD="a: b # c", SLSKD_API_KEY="123456")))
    assert y["web"]["authentication"]["password"] == "a: b # c"
    assert y["web"]["authentication"]["api_keys"]["soularr"]["key"] == "123456"


def test_soularr_ini_seed():
    c = configparser.ConfigParser()
    c.read_string(render(SOULARR_INI, **SLSKD))
    assert dict(c["Lidarr"]) == {"api_key": BASE["LIDARR_API_KEY"], "host_url": "http://172.30.60.13:8686",
                                 "download_dir": "/data/slskd-downloads", "disable_sync": "False"}
    assert dict(c["Slskd"]) == {"api_key": SLSKD["SLSKD_API_KEY"], "host_url": "http://172.30.60.2:5030",
                                "url_base": "/", "download_dir": "/downloads", "delete_searches": "False",
                                "stalled_timeout": "3600"}


# ---- playbooks ------------------------------------------------------------------

def tasks(env):
    out = []
    for play in yaml.safe_load(PLAYBOOKS[env].read_text()):
        if isinstance(play, dict):
            for sec in ("pre_tasks", "tasks"):
                out += play.get(sec) or []
    return out


def _seed(env, src):
    found = [t for t in tasks(env) if "template" in t and src in str(t["template"]["src"])]
    assert len(found) == 1, (env, src)
    return found[0]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
@pytest.mark.parametrize("src", ["slskd.yml.j2", "soularr-config.ini.j2"])
def test_seeds_are_0600_only_if_absent_on_the_media_node_when_enabled(env, src):
    t = _seed(env, src)
    assert str(t["template"]["mode"]) == "0600"
    assert "FORCE_SEED" in str(t["template"]["force"])
    assert t["delegate_to"] == "{{ skfetch.MEDIA_NODE }}"
    assert "SLSKD_ENABLED" in json.dumps(t.get("when"))


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_enabled_requires_the_five_secrets(env):
    found = [t for t in tasks(env) if "assert" in t and "SLSKD_API_KEY" in json.dumps(t)]
    assert len(found) == 1
    text = json.dumps(found[0])
    for k in ("SLSKD_SOULSEEK_USERNAME", "SLSKD_SOULSEEK_PASSWORD", "SLSKD_WEB_USERNAME",
              "SLSKD_WEB_PASSWORD", "SLSKD_API_KEY"):
        assert f"skfetch.{k} | default('') | string) | length > 0" in text, k
    assert "SLSKD_ENABLED" in json.dumps(found[0].get("when"))


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_directories_created_when_enabled(env):
    text = json.dumps([t for t in tasks(env) if "file" in t and "SLSKD_ENABLED" in json.dumps(t.get("when"))])
    assert "slskd-downloads" in text
    assert "{{ skfetch_local_root }}/slskd" in text and "{{ skfetch_local_root }}/soularr" in text


def test_readme_documents_the_knobs():
    r = README.read_text()
    for k in ("SLSKD_ENABLED", "SLSKD_API_KEY", "SOULARR_INTERVAL"):
        assert k in r, k
