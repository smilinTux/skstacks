"""skstream: Plex + Kometa as a swarm stack, DUMB (zurg + rclone FUSE mount +
cli_debrid) as a host-level systemd unit on the media node.

Why host-level: a swarm service on Docker 27 cannot get a device (/dev/fuse),
--privileged, or --security-opt apparmor=unconfined, and all three are needed
for an rclone FUSE mount. Plex stays in swarm and is pinned to the same node
so it sees the mount through rslave propagation.

These tests hold the contract the prod fix of 2026-09 established:
  * images digest-pinned, restart `condition: any`, placement is a knob
  * Plex's SQLite dir is a LOCAL host bind (SQLite on NFS stalls Plex:
    "Waited over 10 seconds for a busy database")
  * secrets live only in 0600 files, never in the compose/unit text
  * the DUMB unit waits for swarm before joining the overlay, is wanted by
    docker.service (a docker restart brings it back) and never gives up
  * DUMB / cli_debrid seed files are written only when absent
  * no recursive chown/chmod over data
"""
import json
import pathlib
import subprocess

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
APP = ANSIBLE / "optional/skstream"
SRC = APP / "src/config/skstream"
COMPOSE = SRC / "skstream.yml.j2"
UNIT = SRC / "skstream-dumb.service.j2"
DUMB_SEED = SRC / "dumb_config.json.j2"
CLID_SEED = SRC / "cli_debrid_config.json.j2"
HOOK = SRC / "plex_update.sh.j2"
WAIT = APP / "src/skstream/sk-wait-swarm-net"
PLAYBOOKS = {e: APP / f"deploy_skstream-{e}.yml" for e in ("dev", "staging", "prod")}
SECRETS = {
    "RD_API_KEY": "RDKEYexample0000000000000000000000000000000000000000",
    "PLEX_TOKEN": "plextokenexample0000",
    "TRAKT_CLIENT_ID": "traktidexample",
    "TRAKT_CLIENT_SECRET": "traktsecretexample",
    "TMDB_API_KEY": "tmdbexample",
}


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    env.filters["to_json"] = json.dumps
    return env


def _vars(env="prod", **over):
    skstream = {"CLUSTERNAME": "demo", "DOMAIN": "example.test", "MEDIA_NODE": "node-a", **SECRETS, **over}
    return {"app": "skstream", "env": env, "skstream": skstream}


def render(path, env="prod", **over):
    return _env().from_string(path.read_text()).render(**_vars(env, **over))


def services(env="prod", **over):
    return yaml.safe_load(render(COMPOSE, env, **over))["services"]


# ---- compose --------------------------------------------------------------

def test_compose_images_default_to_digest_pins():
    svcs = services(KOMETA_ENABLED=True)
    for name in ("plex", "kometa"):
        assert "@sha256:" in svcs[name]["image"], name


def test_compose_image_is_overridable():
    assert services(PLEX_IMAGE="plexinc/pms-docker:1.41.0")["plex"]["image"] == "plexinc/pms-docker:1.41.0"


def test_compose_restart_condition_any_everywhere():
    for name, svc in services(KOMETA_ENABLED=True).items():
        assert svc["deploy"]["restart_policy"]["condition"] == "any", name


def test_placement_default_pins_to_the_media_node_label():
    for svc in services(KOMETA_ENABLED=True).values():
        assert svc["deploy"]["placement"]["constraints"] == ["node.labels.skstream-media == true"]


def test_placement_is_a_knob():
    want = ["node.hostname == other"]
    assert services(placement_constraints=want)["plex"]["deploy"]["placement"]["constraints"] == want


def test_placement_empty_list_is_honoured():
    assert services(placement_constraints=[])["plex"]["deploy"]["placement"]["constraints"] == []


def test_kometa_is_off_by_default():
    assert set(services()) == {"plex"}


def test_kometa_when_enabled_takes_secrets_from_env_file_only():
    k = services(KOMETA_ENABLED=True)["kometa"]
    assert k["env_file"] == "/var/data/config/skstream-prod/kometa.env"
    assert not any("TOKEN" in e or "APIKEY" in e or "SECRET" in e for e in k.get("environment", []))


def test_plex_database_dir_is_a_local_bind_not_nfs():
    vols = services()["plex"]["volumes"]
    db = [v for v in vols if v["target"].endswith("Plug-in Support/Databases")]
    assert len(db) == 1 and db[0]["source"] == "/var/lib/skstream/plex-db"
    assert not db[0]["source"].startswith("/var/data")


def test_plex_sees_the_debrid_mount_read_only_rslave():
    m = [v for v in services()["plex"]["volumes"] if v["target"] == "/mnt/debrid"][0]
    assert m["read_only"] is True and m["bind"]["propagation"] == "rslave"
    assert m["source"] == "/var/data/skstream-prod/zurg/mnt"


def test_plex_stop_grace_and_traefik_router():
    p = services()["plex"]
    assert p["stop_grace_period"] == "60s"
    labels = p["deploy"]["labels"]
    assert "traefik.http.routers.skstream.rule=Host(`skstream.demo.example.test`)" in labels
    assert "traefik.http.services.skstream.loadbalancer.server.port=32400" in labels


def test_non_prod_router_and_paths_are_env_suffixed():
    p = services(env="dev")["plex"]
    assert "traefik.http.routers.skstream-dev.rule=Host(`skstream-dev.demo.example.test`)" in p["deploy"]["labels"]
    db = [v for v in p["volumes"] if v["target"].endswith("Databases")][0]
    assert db["source"] == "/var/lib/skstream-dev/plex-db"


def test_no_secret_value_lands_in_the_compose_text():
    text = render(COMPOSE, KOMETA_ENABLED=True, PLEX_CLAIM="claim-example-123")
    for value in list(SECRETS.values()) + ["claim-example-123"]:
        assert value not in text


# ---- DUMB systemd unit ----------------------------------------------------

def unit(env="prod", **over):
    return render(UNIT, env, **over)


def test_unit_waits_for_swarm_and_is_wanted_by_docker():
    u = unit()
    assert "ExecStartPre=/usr/local/sbin/sk-wait-swarm-net skstream-prod" in u
    assert "WantedBy=multi-user.target docker.service" in u
    assert "StartLimitIntervalSec=0" in u and "Restart=always" in u


def test_unit_has_what_fuse_needs():
    u = unit()
    assert "--device /dev/fuse:/dev/fuse:rwm" in u
    assert "--cap-add SYS_ADMIN" in u
    assert "--security-opt apparmor:unconfined" in u
    assert "-v /var/data/skstream-prod/zurg/mnt:/mnt/debrid:rshared" in u


def test_unit_keeps_sqlite_state_local_and_config_on_nfs():
    u = unit()
    assert "-v /var/lib/skstream/dumb-data:/data" in u
    assert "-v /var/lib/skstream/dumb-log:/log" in u
    assert "-v /var/data/skstream-prod/dumb/config:/config" in u


def test_unit_image_digest_pinned_and_no_secrets():
    u = unit()
    assert "iampuid0/dumb@sha256:" in u
    for value in SECRETS.values():
        assert value not in u


def test_unit_name_and_container_are_env_scoped_outside_prod():
    u = unit(env="staging")
    assert "--name skstream-dumb-staging" in u and "skstream-staging" in u


# ---- seeds ----------------------------------------------------------------

def test_dumb_seed_is_valid_json_with_the_debrid_chain_enabled():
    d = json.loads(render(DUMB_SEED))
    assert d["zurg"]["instances"]["RealDebrid"]["enabled"] is True
    assert d["rclone"]["instances"]["RealDebrid"]["enabled"] is True
    assert d["cli_debrid"]["enabled"] is True and d["cli_battery"]["enabled"] is True
    assert d["zurg"]["instances"]["RealDebrid"]["api_key"] == SECRETS["RD_API_KEY"]
    assert d["dumb"]["plex_token"] == SECRETS["PLEX_TOKEN"]
    assert d["dumb"]["plex_address"] == "http://plex:32400"


def test_dumb_seed_survives_awkward_secret_characters():
    d = json.loads(render(DUMB_SEED, RD_API_KEY='a"b\\c'))
    assert d["zurg"]["instances"]["RealDebrid"]["api_key"] == 'a"b\\c'


def test_cli_debrid_seed_watchlist_torrentio_rd_trakt():
    c = json.loads(render(CLID_SEED))
    src = c["Content Sources"]["My Plex Watchlist_1"]
    assert src["enabled"] is True and src["cutoff_date"] == "" and src["versions"] == ["1080p"]
    assert c["Scrapers"]["Torrentio_1"]["enabled"] is True
    assert c["Debrid Provider"] == {"provider": "RealDebrid", "api_key": SECRETS["RD_API_KEY"]}
    assert c["Trakt"] == {"client_id": SECRETS["TRAKT_CLIENT_ID"], "client_secret": SECRETS["TRAKT_CLIENT_SECRET"]}
    assert c["Debug"]["auto_run_program"] is True


def test_cli_debrid_cutoff_date_is_a_knob():
    c = json.loads(render(CLID_SEED, CUTOFF_DATE="7"))
    assert c["Content Sources"]["My Plex Watchlist_1"]["cutoff_date"] == "7"


def test_cli_debrid_seed_paths_follow_the_mount_name():
    c = json.loads(render(CLID_SEED, RCLONE_MOUNT_NAME="pd_zurg"))
    assert c["Plex"]["mounted_file_location"] == "/mnt/debrid/pd_zurg/__all__"


# ---- scripts ----------------------------------------------------------------

def test_hook_reads_token_at_runtime_and_parses(tmp_path):
    text = render(HOOK, RCLONE_MOUNT_NAME="pd_zurg")
    assert SECRETS["PLEX_TOKEN"] not in text
    assert "/config/dumb_config.json" in text and "/mnt/debrid/pd_zurg/" in text
    f = tmp_path / "hook.sh"
    f.write_text(text)
    assert subprocess.run(["sh", "-n", str(f)]).returncode == 0


def test_wait_script_parses_and_times_out_without_docker(tmp_path):
    assert subprocess.run(["sh", "-n", str(WAIT)]).returncode == 0
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    r = subprocess.run(["sh", str(WAIT), "net", "2"], env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "swarm not active" in r.stdout


# ---- playbooks --------------------------------------------------------------

def tasks(env):
    out = []
    for play in yaml.safe_load(PLAYBOOKS[env].read_text()):
        if isinstance(play, dict):
            for sec in ("pre_tasks", "tasks"):
                out += play.get(sec) or []
    return out


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_required_secrets_fail_closed(env):
    asserts = [t for t in tasks(env) if "assert" in t]
    text = json.dumps(asserts)
    for key in ("RD_API_KEY", "PLEX_TOKEN", "MEDIA_NODE"):
        assert key in text, key


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_seed_templates_never_clobber_runtime_state(env):
    seeds = [t for t in tasks(env) if "template" in t and any(
        s in str(t["template"].get("src", "")) for s in ("dumb_config.json", "cli_debrid_config.json"))]
    assert len(seeds) == 2
    for t in seeds:
        assert "FORCE_SEED" in str(t["template"].get("force")), t["name"]
        assert str(t["template"]["mode"]) == "0600"


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_no_recursive_ownership_or_mode_changes(env):
    for t in tasks(env):
        for mod in ("file", "ansible.builtin.file"):
            if mod in t:
                assert not t[mod].get("recurse"), t["name"]
        assert "chown -R" not in json.dumps(t) and "chmod -R" not in json.dumps(t), t["name"]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_host_level_tasks_run_on_the_media_node(env):
    host = [t for t in tasks(env) if "systemd" in t or "skstream-dumb.service" in json.dumps(t)]
    assert host
    for t in host:
        assert t.get("delegate_to") == "{{ skstream.MEDIA_NODE }}", t["name"]


# ---- deploy script ------------------------------------------------------------

DEPLOY = APP / "src/skstream/deploy.j2"


def _deploy(**over):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)  # plain, like the repo-wide deploy tests
    return env.from_string(DEPLOY.read_text()).render(**_vars(**over))


def test_deploy_script_force_updates_plex_only_when_it_preexisted():
    s = _deploy()
    assert "record_preexisting \"${STACK_NAME}_plex\"" in s
    assert "preexisted ${STACK_NAME}_plex &&" in s


@pytest.mark.parametrize("flag,present", [(True, True), ("true", True), (False, False), ("no", False)])
def test_deploy_script_touches_kometa_only_when_enabled(flag, present):
    assert ("preexisted ${STACK_NAME}_kometa" in _deploy(KOMETA_ENABLED=flag)) is present


def test_deploy_script_uses_the_skstream_network_default():
    assert 'NETWORKS["skstream-prod"]="172.16.162.0/24"' in _deploy()


# ---- follow-up: hybrid mode, PLEX_TOKEN guard -----------------------------------

def test_cli_debrid_hybrid_mode_defaults_on():
    c = json.loads(render(CLID_SEED))
    assert c["Scraping"]["hybrid_mode"] is True


@pytest.mark.parametrize("flag", [False, "false", "no"])
def test_cli_debrid_hybrid_mode_can_be_turned_off(flag):
    c = json.loads(render(CLID_SEED, CLI_DEBRID_HYBRID_MODE=flag))
    assert c["Scraping"]["hybrid_mode"] is False


def test_cli_debrid_seed_stays_valid_json_with_hybrid_on():
    c = json.loads(render(CLID_SEED, CLI_DEBRID_HYBRID_MODE=True))
    assert c["Scraping"]["uncached_content_handling"] == "None"


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_play_refuses_a_plex_server_token(env):
    text = json.dumps([t for t in tasks(env) if "assert" in t])
    assert "PLEX_TOKEN_IS_SERVER_TOKEN" in text


def test_readme_warns_plex_token_is_never_the_server_token():
    readme = (APP / "README.md").read_text()
    assert "never the server `PlexOnlineToken`" in readme
    assert "plex.tv/link" in readme and "### Minting the PLEX_TOKEN" in readme
    assert "the Plex server's own token" not in readme


def test_example_vault_comment_says_dedicated_token():
    ex = (ANSIBLE.parent / "tests/render/vars/skstream.example.yml").read_text()
    assert "never the server PlexOnlineToken" in ex


def test_readme_notes_dmca_blocked_cached_releases():
    readme = (APP / "README.md").read_text()
    assert "451" in readme and "infringing_file" in readme
