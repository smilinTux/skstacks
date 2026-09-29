"""skfetch: Sonarr / Radarr / Lidarr / Prowlarr (+ FlareSolverr) and
qBittorrent behind a VPN (gluetun), as a host-level docker compose project
started by a systemd unit on the media node.

Why host-level: qBittorrent shares gluetun's network namespace
(`network_mode: service:gluetun`), which swarm does not support, and gluetun
needs NET_ADMIN plus /dev/net/tun. Contract held here:
  * images digest-pinned, mem_limit + log caps + restart on-failure everywhere
  * VPN credentials only in the 0600 env file, never in compose/unit text
  * incomplete downloads live on the media dataset, *arr SQLite on local disk
  * config seeds (qBittorrent.conf, config.xml) only if absent
  * *arr auth is External (the reverse proxy authenticates) and AllowedHosts
    includes the public FQDN (without it current *arr versions answer 400)
  * post-deploy API wiring is idempotent
"""
import json
import pathlib
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
APP = ANSIBLE / "optional/skfetch"
SRC = APP / "src/config/skfetch"
COMPOSE = SRC / "skfetch.yml.j2"
ENVFILE = SRC / "skfetch.env.j2"
UNIT = SRC / "skfetch.service.j2"
QBT = SRC / "qBittorrent.conf.j2"
ARR = SRC / "arr-config.xml.j2"
TRAEFIK = SRC / "skfetch-traefik.yml.j2"
WIRE_CFG = SRC / "wire.json.j2"
WIRE = APP / "src/skfetch/wire.py"
PLAYBOOKS = {e: APP / f"deploy_skfetch-{e}.yml" for e in ("dev", "staging", "prod")}
SECRETS = {
    "OPENVPN_USER": "vpnuserexample",
    "OPENVPN_PASSWORD": "vpnpassexample",
    "SONARR_API_KEY": "sonarrkeyexample",
    "RADARR_API_KEY": "radarrkeyexample",
    "LIDARR_API_KEY": "lidarrkeyexample",
    "PROWLARR_API_KEY": "prowlarrkeyexample",
    "PLEX_TOKEN": "plextokenexample",
}
SERVICES = {"gluetun", "qbittorrent", "prowlarr", "sonarr", "radarr", "lidarr", "flaresolverr"}


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    env.filters["to_json"] = json.dumps
    return env


def _vars(env="prod", **over):
    skfetch = {"CLUSTERNAME": "demo", "DOMAIN": "example.test", "MEDIA_NODE": "node-a",
               "LAN_BIND_ADDRESS": "192.0.2.10", **SECRETS, **over}
    return {"app": "skfetch", "env": env, "skfetch": skfetch}


def render(path, env="prod", extra=None, **over):
    return _env().from_string(path.read_text()).render(**_vars(env, **over), **(extra or {}))


def compose(env="prod", **over):
    return yaml.safe_load(render(COMPOSE, env, **over))


# ---- compose ----------------------------------------------------------------

def test_compose_has_every_service_digest_pinned():
    svcs = compose()["services"]
    assert set(svcs) == SERVICES
    for name, svc in svcs.items():
        assert "@sha256:" in svc["image"], name


def test_every_service_has_mem_limit_log_caps_and_restart_on_failure():
    for name, svc in compose()["services"].items():
        assert svc["restart"] == "on-failure", name
        assert svc["mem_limit"], name
        assert svc["logging"]["options"] == {"max-size": "10m", "max-file": "3"}, name


def test_qbittorrent_shares_the_vpn_namespace_and_waits_for_it():
    q = compose()["services"]["qbittorrent"]
    assert q["network_mode"] == "service:gluetun"
    assert q["depends_on"] == {"gluetun": {"condition": "service_healthy"}}
    assert "ports" not in q and "networks" not in q


def test_gluetun_has_what_a_tunnel_needs_and_secrets_come_from_env_file():
    g = compose()["services"]["gluetun"]
    assert g["cap_add"] == ["NET_ADMIN"] and g["devices"] == ["/dev/net/tun:/dev/net/tun"]
    assert g["env_file"] == "/var/data/config/skfetch-prod/skfetch.env"
    e = g["environment"]
    assert e["VPN_SERVICE_PROVIDER"] == "private internet access" and e["VPN_TYPE"] == "openvpn"
    assert e["VPN_PORT_FORWARDING"] == "on" and e["HTTP_CONTROL_SERVER_ADDRESS"] == ":8000"
    assert "{{PORTS}}" in e["VPN_PORT_FORWARDING_UP_COMMAND"]
    assert "127.0.0.1:8080/api/v2/app/setPreferences" in e["VPN_PORT_FORWARDING_UP_COMMAND"]


def test_vpn_provider_and_regions_are_knobs():
    e = compose(VPN_SERVICE_PROVIDER="mullvad", SERVER_REGIONS="Sweden")["services"]["gluetun"]["environment"]
    assert e["VPN_SERVICE_PROVIDER"] == "mullvad" and e["SERVER_REGIONS"] == "Sweden"


def test_no_secret_value_in_the_compose_text():
    text = render(COMPOSE)
    for v in SECRETS.values():
        assert v not in text


def test_media_on_the_dataset_and_sqlite_on_local_disk():
    svcs = compose()["services"]
    for name in ("sonarr", "radarr", "lidarr", "qbittorrent"):
        assert "/var/data/skfetch-prod:/data" in svcs[name]["volumes"], name
    for name in ("sonarr", "radarr", "lidarr", "prowlarr", "qbittorrent"):
        assert f"/var/lib/skfetch/{name}:/config" in svcs[name]["volumes"], name
    assert not any("incomplete" in v for v in svcs["qbittorrent"]["volumes"])


def test_media_path_and_local_root_are_knobs_and_env_scoped():
    svcs = compose(env="dev", MEDIA_PATH="/tank/fetch")["services"]
    assert "/tank/fetch:/data" in svcs["sonarr"]["volumes"]
    assert "/var/lib/skfetch-dev/sonarr:/config" in svcs["sonarr"]["volumes"]


def test_web_uis_bind_only_the_lan_address():
    svcs = compose()["services"]
    assert svcs["sonarr"]["ports"] == ["192.0.2.10:8989:8989"]
    assert svcs["prowlarr"]["ports"] == ["192.0.2.10:9696:9696"]
    assert "ports" not in svcs["gluetun"] and "ports" not in svcs["flaresolverr"]


def test_bridge_ips_follow_the_subnet_knob():
    doc = compose(BRIDGE_SUBNET="10.99.7.0/24")
    assert doc["networks"]["skfetch"]["ipam"]["config"] == [{"subnet": "10.99.7.0/24"}]
    assert doc["services"]["gluetun"]["networks"]["skfetch"]["ipv4_address"] == "10.99.7.2"
    assert doc["services"]["sonarr"]["networks"]["skfetch"]["ipv4_address"] == "10.99.7.11"


def test_arrs_join_the_stream_overlay_by_default():
    doc = compose()
    assert doc["networks"]["skstream-prod"] == {"external": True}
    for name in ("sonarr", "radarr", "lidarr"):
        assert "skstream-prod" in doc["services"][name]["networks"]


def test_stream_overlay_can_be_turned_off():
    doc = compose(STREAM_NETWORK="")
    assert set(doc["networks"]) == {"skfetch"}
    assert "skstream-prod" not in doc["services"]["sonarr"]["networks"]


@pytest.mark.parametrize("flag", [False, "false"])
def test_flaresolverr_toggle_off(flag):
    assert "flaresolverr" not in compose(FLARESOLVERR_ENABLED=flag)["services"]


# ---- env file ---------------------------------------------------------------

def test_env_file_carries_the_vpn_credentials():
    text = render(ENVFILE)
    assert "OPENVPN_USER=vpnuserexample" in text and "OPENVPN_PASSWORD=vpnpassexample" in text
    assert "WIREGUARD_PRIVATE_KEY" not in text


def test_env_file_wireguard_key_only_when_set():
    assert "WIREGUARD_PRIVATE_KEY=wgexample" in render(ENVFILE, WIREGUARD_PRIVATE_KEY="wgexample")


# ---- qBittorrent seed -------------------------------------------------------

def qbt(**over):
    return render(QBT, **over).splitlines()


def test_qbittorrent_seed_binds_to_the_tunnel_and_keeps_incomplete_on_the_dataset():
    c = qbt()
    for line in ("Session\\Interface=tun0", "Session\\InterfaceName=tun0", "Session\\AnonymousModeEnabled=true",
                 "Session\\DefaultSavePath=/data/downloads/", "Session\\TempPath=/data/incomplete",
                 "Session\\TempPathEnabled=true", "WebUI\\AuthSubnetWhitelist=172.30.60.0/24",
                 "WebUI\\AuthSubnetWhitelistEnabled=true", "Session\\ShareLimitAction=Stop"):
        assert line in c, line


def test_qbittorrent_seed_defaults_ratio_one_sixty_minutes():
    c = qbt()
    assert "Session\\GlobalMaxRatio=1" in c and "Session\\GlobalMaxSeedingMinutes=60" in c
    assert "Session\\GlobalDLSpeedLimit=25600" in c and "Session\\MaxActiveDownloads=2" in c


def test_qbittorrent_seed_knobs():
    c = qbt(QBT_DL_LIMIT_KIB=0, QBT_MAX_ACTIVE_TORRENTS=9, BRIDGE_SUBNET="10.99.7.0/24")
    assert "Session\\GlobalDLSpeedLimit=0" in c and "Session\\MaxActiveTorrents=9" in c
    assert "WebUI\\AuthSubnetWhitelist=10.99.7.0/24" in c


# ---- *arr config.xml seeds ----------------------------------------------------

ARRS = {"sonarr": (8989, "11"), "radarr": (7878, "12"), "lidarr": (8686, "13"), "prowlarr": (9696, "10")}


def arr(name, env="prod", **over):
    port, octet = ARRS[name]
    extra = {"arr": {"name": name, "port": port, "octet": octet}}
    return ET.fromstring(render(ARR, env, extra=extra, **over))


@pytest.mark.parametrize("name", sorted(ARRS))
def test_arr_seed_uses_external_auth_and_the_vault_api_key(name):
    x = arr(name)
    assert x.findtext("AuthenticationMethod") == "External"
    assert x.findtext("AuthenticationRequired") == "Enabled"
    assert x.findtext("ApiKey") == SECRETS[f"{name.upper()}_API_KEY"]
    assert x.findtext("Port") == str(ARRS[name][0])


@pytest.mark.parametrize("name", sorted(ARRS))
def test_arr_allowed_hosts_include_the_public_fqdn(name):
    hosts = arr(name).findtext("AllowedHosts").split(",")
    assert f"{name}.demo.example.test" in hosts
    assert {"192.0.2.10", "node-a", "localhost", "127.0.0.1", f"172.30.60.{ARRS[name][1]}"} <= set(hosts)


def test_arr_allowed_hosts_non_prod_fqdn_is_suffixed():
    assert "sonarr-dev.demo.example.test" in arr("sonarr", env="dev").findtext("AllowedHosts").split(",")


def test_arr_seed_escapes_xml():
    assert arr("sonarr", SONARR_API_KEY="a<b&c").findtext("ApiKey") == "a<b&c"


# ---- systemd unit -------------------------------------------------------------

def test_unit_is_a_hardened_oneshot_compose_wrapper():
    u = render(UNIT)
    for line in ("Type=oneshot", "RemainAfterExit=yes", "StartLimitIntervalSec=0",
                 "ExecStartPre=/usr/local/sbin/sk-wait-swarm-net skstream-prod",
                 "WantedBy=multi-user.target docker.service",
                 "RequiresMountsFor=/var/data/skfetch-prod /var/data/config/skfetch-prod",
                 "ExecStart=/usr/bin/docker compose -p skfetch-prod -f /var/data/config/skfetch-prod/docker-compose.yml up -d --remove-orphans",
                 "ExecStop=/usr/bin/docker compose -p skfetch-prod -f /var/data/config/skfetch-prod/docker-compose.yml down"):
        assert line in u, line


def test_unit_skips_the_swarm_wait_without_a_stream_overlay():
    assert "sk-wait-swarm-net" not in render(UNIT, STREAM_NETWORK="")


def test_unit_has_no_secrets():
    u = render(UNIT)
    for v in SECRETS.values():
        assert v not in u


# ---- traefik ------------------------------------------------------------------

def traefik(**over):
    return yaml.safe_load(render(TRAEFIK, **over))["http"]


def test_traefik_routes_each_ui_through_authentik_by_default():
    h = traefik()
    assert set(h["routers"]) == {"skfetch-sonarr", "skfetch-radarr", "skfetch-lidarr", "skfetch-prowlarr"}
    r = h["routers"]["skfetch-sonarr"]
    assert r["rule"] == "Host(`sonarr.demo.example.test`)"
    assert r["middlewares"] == ["default-security-headers@file", "authentik@file"]
    assert r["tls"] == {"certResolver": "main"}
    assert h["services"]["skfetch-sonarr"]["loadBalancer"]["servers"] == [{"url": "http://192.0.2.10:8989"}]


def test_traefik_middlewares_are_a_knob():
    assert traefik(TRAEFIK_MIDDLEWARES=["sso@file"])["routers"]["skfetch-radarr"]["middlewares"] == ["sso@file"]


def test_traefik_never_routes_qbittorrent():
    assert "qbittorrent" not in render(TRAEFIK)


# ---- playbooks ------------------------------------------------------------------

def tasks(env):
    out = []
    for play in yaml.safe_load(PLAYBOOKS[env].read_text()):
        if isinstance(play, dict):
            for sec in ("pre_tasks", "tasks"):
                out += play.get(sec) or []
    return out


def _dest(t):
    return str((t.get("template") or t.get("copy") or {}).get("dest", "")).replace("{{ app }}", "skfetch")


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_required_vars_fail_closed(env):
    text = json.dumps([t for t in tasks(env) if "assert" in t])
    for key in ("OPENVPN_USER", "OPENVPN_PASSWORD", "MEDIA_NODE", "SONARR_API_KEY", "RADARR_API_KEY",
                "LIDARR_API_KEY", "PROWLARR_API_KEY", "PLEX_TOKEN_IS_SERVER_TOKEN"):
        assert key in text, key


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_secret_files_are_0600(env):
    ts = {pathlib.PurePath(_dest(t)).name: t for t in tasks(env) if _dest(t)}
    for name in ("skfetch.env", "wire.json"):
        t = ts[name]
        assert str((t.get("template") or t.get("copy"))["mode"]) == "0600", name
    arr_t = [t for t in tasks(env) if "arr-config.xml" in json.dumps(t)]
    assert len(arr_t) == 1 and str(arr_t[0]["template"]["mode"]) == "0600"


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_seeds_never_clobber_runtime_state(env):
    seeds = [t for t in tasks(env) if "template" in t and any(
        s in str(t["template"]["src"]) for s in ("qBittorrent.conf", "arr-config.xml"))]
    assert len(seeds) == 2
    for t in seeds:
        assert "FORCE_SEED" in str(t["template"]["force"]), t["name"]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_no_recursive_ownership_or_mode_changes(env):
    for t in tasks(env):
        if "file" in t:
            assert not t["file"].get("recurse"), t["name"]
        assert "chown -R" not in json.dumps(t) and "chmod -R" not in json.dumps(t), t["name"]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_host_tasks_run_on_the_media_node(env):
    host = [t for t in tasks(env) if "systemd" in t or "/var/lib/skfetch" in json.dumps(t)
            or "skfetch_local_root" in json.dumps(t) or "wire.py" in str(t.get("shell", ""))]
    assert len(host) >= 4
    for t in host:
        assert t.get("delegate_to") == "{{ skfetch.MEDIA_NODE }}", t["name"]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_traefik_file_only_when_a_dynamic_dir_is_set(env):
    t = [t for t in tasks(env) if "skfetch-traefik.yml" in json.dumps(t)]
    assert len(t) == 1 and "TRAEFIK_DYNAMIC_DIR" in json.dumps(t[0].get("when"))


# ---- wiring script ------------------------------------------------------------------

def _schema(impl, fields, **top):
    return {"implementation": impl, "name": impl, "fields": [{"name": f, "value": None} for f in fields], **top}


class Fake:
    """In-memory Servarr/Prowlarr API: lists per path, POST appends."""

    def __init__(self, kind):
        self.kind = kind
        self.data = {}
        self.posts = []
        self.fail_plex = False
        ver = "v1" if kind in ("lidarr", "prowlarr") else "v3"
        self.ver = ver
        cat = {"sonarr": "tvCategory", "radarr": "movieCategory", "lidarr": "musicCategory"}.get(kind)
        self.schemas = {
            f"/api/{ver}/downloadclient/schema": [_schema("QBittorrent", ["host", "port", cat], removeCompletedDownloads=False)],
            f"/api/{ver}/notification/schema": [_schema("PlexServer", ["host", "port", "authToken", "updateLibrary", "mapFrom", "mapTo"],
                                                        onDownload=False, supportsOnDownload=True, onUpgrade=False, supportsOnUpgrade=True)],
            "/api/v1/applications/schema": [_schema(a, ["prowlarrUrl", "baseUrl", "apiKey"], syncLevel="addOnly") for a in ("Sonarr", "Radarr", "Lidarr")],
            "/api/v1/indexerProxy/schema": [_schema("FlareSolverr", ["host", "requestTimeout"])],
            "/api/v1/indexer/schema": [dict(_schema("Cardigann", ["definitionFile"]), name=n, definitionName=d)
                                       for n, d in (("The Pirate Bay", "thepiratebay"), ("EZTV", "eztv"), ("knaben", "knaben"))],
        }
        self.data.update({f"/api/{ver}/qualityprofile": [{"id": 4}], "/api/v1/metadataprofile": [{"id": 7}],
                          "/api/v1/appprofile": [{"id": 3}]})


def serve(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = self.path.split("?")[0]
            if self.headers.get("X-Api-Key") != "k":
                return self._send(401, {})
            if path.endswith("/system/status"):
                return self._send(200, {"version": "x"})
            self._send(200, fake.schemas.get(path, fake.data.get(path, [])))

        def do_POST(self):
            path = self.path.split("?")[0]
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if fake.fail_plex and body.get("implementation") == "PlexServer":
                return self._send(400, [{"errorMessage": "no music library"}])
            body["id"] = len(fake.data.setdefault(path, [])) + 1
            fake.data[path].append(body)
            fake.posts.append((path, body))
            self._send(201, body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def cluster(tmp_path):
    fakes = {k: Fake(k) for k in ("sonarr", "radarr", "lidarr", "prowlarr")}
    srvs = {k: serve(f) for k, f in fakes.items()}
    url = {k: f"http://127.0.0.1:{s.server_address[1]}" for k, s in srvs.items()}
    cfg = json.loads(render(WIRE_CFG, SONARR_API_KEY="k", RADARR_API_KEY="k", LIDARR_API_KEY="k", PROWLARR_API_KEY="k",
                            INDEXERS=["thepiratebay", "Knaben", "nosuchindexer"], INDEXERS_FLARESOLVERR=["eztv"]))
    for k in fakes:
        target = cfg["prowlarr"] if k == "prowlarr" else cfg["apps"][k]
        target["url"] = url[k]
    cfg["wait"] = {"tries": 2, "delay": 0}
    path = tmp_path / "wire.json"
    path.write_text(json.dumps(cfg))
    yield fakes, path
    for s in srvs.values():
        s.shutdown()


def run_wire(path):
    return subprocess.run([sys.executable, str(WIRE), str(path)], capture_output=True, text=True, timeout=60)


def test_wire_config_is_valid_json_with_the_expected_shape():
    cfg = json.loads(render(WIRE_CFG))
    assert cfg["qbittorrent"] == {"host": "172.30.60.2", "port": 8080}
    assert cfg["apps"]["sonarr"]["root"] == "/data/media/tv" and cfg["apps"]["lidarr"]["api"] == "v1"
    assert cfg["plex"]["map_from"] == "/data/media/" and cfg["plex"]["map_to"] == "/skfetch/media/"
    assert cfg["flaresolverr"]["url"] == "http://172.30.60.20:8191/"
    assert cfg["indexers"] == ["thepiratebay", "yts", "nyaasi", "limetorrents", "Knaben", "torrentdownload"]
    assert cfg["indexers_flaresolverr"] == ["eztv", "1337x", "uindex"]


def test_wire_config_drops_plex_and_flaresolverr_when_off():
    cfg = json.loads(render(WIRE_CFG, PLEX_TOKEN="", FLARESOLVERR_ENABLED=False))
    assert cfg["plex"] is None and cfg["flaresolverr"] is None


def test_wire_creates_everything_once(cluster):
    fakes, path = cluster
    r = run_wire(path)
    assert r.returncode == 0, r.stdout + r.stderr
    s = fakes["sonarr"].data
    assert s["/api/v3/rootfolder"][0]["path"] == "/data/media/tv"
    dc = s["/api/v3/downloadclient"][0]
    fields = {f["name"]: f["value"] for f in dc["fields"]}
    assert fields == {"host": "172.30.60.2", "port": 8080, "tvCategory": "tv"} and dc["removeCompletedDownloads"] is True
    plex = s["/api/v3/notification"][0]
    pf = {f["name"]: f["value"] for f in plex["fields"]}
    assert pf["host"] == "plex" and pf["authToken"] == SECRETS["PLEX_TOKEN"] and pf["mapTo"] == "/skfetch/media/"
    assert plex["onDownload"] is True
    lid = fakes["lidarr"].data["/api/v1/rootfolder"][0]
    assert lid["defaultQualityProfileId"] == 4 and lid["defaultMetadataProfileId"] == 7
    p = fakes["prowlarr"].data
    apps = {a["implementation"]: a for a in p["/api/v1/applications"]}
    assert set(apps) == {"Sonarr", "Radarr", "Lidarr"} and apps["Sonarr"]["syncLevel"] == "fullSync"
    tag = p["/api/v1/tag"][0]
    assert tag["label"] == "flaresolverr" and p["/api/v1/indexerProxy"][0]["tags"] == [tag["id"]]
    idx = {i["definitionName"]: i for i in p["/api/v1/indexer"]}
    assert set(idx) == {"thepiratebay", "knaben", "eztv"}
    assert idx["eztv"]["tags"] == [tag["id"]] and idx["thepiratebay"]["tags"] == []
    assert idx["knaben"]["appProfileId"] == 3 and idx["knaben"]["enable"] is True
    assert "nosuchindexer" in r.stdout


def test_wire_second_run_changes_nothing(cluster):
    fakes, path = cluster
    assert run_wire(path).returncode == 0
    before = sum(len(f.posts) for f in fakes.values())
    r = run_wire(path)
    assert r.returncode == 0 and sum(len(f.posts) for f in fakes.values()) == before
    assert "created" not in r.stdout


def test_wire_plex_notification_failure_is_only_a_warning(cluster):
    fakes, path = cluster
    fakes["lidarr"].fail_plex = True
    r = run_wire(path)
    assert r.returncode == 0 and "WARN" in r.stdout and "lidarr" in r.stdout


def test_wire_fails_when_an_api_never_answers(tmp_path):
    cfg = json.loads(render(WIRE_CFG))
    cfg["apps"]["sonarr"]["url"] = "http://127.0.0.1:9"
    cfg["wait"] = {"tries": 1, "delay": 0}
    p = tmp_path / "w.json"
    p.write_text(json.dumps(cfg))
    r = run_wire(p)
    assert r.returncode != 0 and "sonarr" in r.stdout + r.stderr


# ---- skstream follow-up ----------------------------------------------------------

SKSTREAM = ANSIBLE / "optional/skstream/src/config/skstream/skstream.yml.j2"


def _plex(**over):
    v = {"app": "skstream", "env": "prod",
         "skstream": {"CLUSTERNAME": "demo", "DOMAIN": "example.test", "MEDIA_NODE": "n", **over}}
    return yaml.safe_load(_env().from_string(SKSTREAM.read_text()).render(**v))["services"]["plex"]


def test_skstream_has_no_extra_plex_bind_by_default():
    assert not any(v["target"] == "/skfetch/media" for v in _plex()["volumes"])


def test_skstream_extra_plex_bind_is_read_only():
    vols = _plex(PLEX_EXTRA_BINDS=[{"source": "/var/data/skfetch-prod/media", "target": "/skfetch/media"}])["volumes"]
    b = [v for v in vols if v["target"] == "/skfetch/media"]
    assert b == [{"type": "bind", "source": "/var/data/skfetch-prod/media", "target": "/skfetch/media", "read_only": True}]


def test_skstream_readme_documents_extra_binds_and_music_library():
    readme = (ANSIBLE / "optional/skstream/README.md").read_text()
    assert "PLEX_EXTRA_BINDS" in readme and "Music library" in readme


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_required_var_checks_survive_an_all_digit_key(env):
    """An all-digit API key loads from YAML as an int; `| length` on an int
    crashed the play in the render gate. Every required check casts first."""
    asserts = [t for t in tasks(env) if "assert" in t][0]["assert"]["that"]
    for cond in asserts:
        if "length > 0" in cond:
            assert "| string) | length > 0" in cond, cond
