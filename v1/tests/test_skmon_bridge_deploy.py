"""skmon-bridge deployment (Task 13): the estate's pinned sk-alert, its routing and secrets,
the out-of-domain channel guard and the healthz port."""
import json
import re

import pytest
import yaml

from skmon_support import (BASE, CFG, FAKE_SK_ALERT, GUARDS, PLAYS, ROLE, SECRET_TEMPLATES, compose, ctx_with_configs,
                           eval_asserts, guard_facts, play_tasks, render, services, task_named)

facts = guard_facts
UID = 65534


def env_of(service: dict) -> list[str]:
    return service["environment"]


def msgs(**kw) -> list[str]:
    return eval_asserts(GUARDS, facts(**kw))


# --- guards ------------------------------------------------------------------------

def test_hash_mismatch_fails_the_play():
    got = msgs(skalert_actual_sha256="0" * 64)
    assert any("SKALERT_SCRIPT_SHA256" in m for m in got)
    m = next(m for m in got if "SKALERT_SCRIPT_SHA256" in m)
    assert "0" * 12 in m and BASE["SKALERT_SCRIPT_SHA256"][:12] in m
    assert "0" * 13 not in m and BASE["SKALERT_SCRIPT_SHA256"][:13] not in m


def test_matching_hash_passes():
    assert msgs() == []


def test_missing_script_names_skalert_script_src():
    got = msgs(skalert_actual_sha256="")
    assert any("SKALERT_SCRIPT_SRC" in m for m in got)
    assert not any("does not match" in m for m in got)


def test_script_src_and_pin_are_required():
    assert any("SKALERT_SCRIPT_SRC" in m for m in msgs(SKALERT_SCRIPT_SRC=""))
    assert any("SKALERT_SCRIPT_SHA256" in m for m in msgs(SKALERT_SCRIPT_SHA256=""))


def test_out_of_domain_channel_required():
    assert any("ALERT_OUT_OF_DOMAIN_CHANNEL" in m for m in msgs(ALERT_OUT_OF_DOMAIN_CHANNEL=""))
    assert any("ALERT_OUT_OF_DOMAIN_CHANNEL" in m for m in msgs(ALERT_OUT_OF_DOMAIN_CHANNEL="email"))


@pytest.mark.parametrize("ch", ["telegram", "talk-external"])
def test_out_of_domain_channels_accepted(ch):
    assert msgs(ALERT_OUT_OF_DOMAIN_CHANNEL=ch) == []


def test_in_domain_only_needs_a_reason():
    assert msgs(ALERT_OUT_OF_DOMAIN_CHANNEL="", ALERT_ACCEPT_IN_DOMAIN_ONLY=True, ALERT_ACCEPT_IN_DOMAIN_ONLY_REASON="lab") == []
    assert msgs(ALERT_OUT_OF_DOMAIN_CHANNEL="", ALERT_ACCEPT_IN_DOMAIN_ONLY=True) != []
    assert msgs(ALERT_OUT_OF_DOMAIN_CHANNEL="", ALERT_ACCEPT_IN_DOMAIN_ONLY="true", ALERT_ACCEPT_IN_DOMAIN_ONLY_REASON="lab") == []
    assert msgs(ALERT_OUT_OF_DOMAIN_CHANNEL="", ALERT_ACCEPT_IN_DOMAIN_ONLY=False, ALERT_ACCEPT_IN_DOMAIN_ONLY_REASON="lab") != []


def test_healthz_allow_required_when_unparked():
    assert any("HEALTHZ_ALLOW" in m for m in msgs(HEALTHZ_ALLOW=[]))
    assert not any("HEALTHZ_ALLOW" in m for m in msgs(HEALTHZ_ALLOW=[], replicas=0))
    assert not any("HEALTHZ_ALLOW" in m for m in msgs(HEALTHZ_ALLOW=[], replicas="0"))


@pytest.mark.parametrize("key", ["ALERT_ROUTING", "ALERT_SECRETS"])
def test_routing_and_secrets_must_be_non_empty(key):
    assert any(key in m for m in msgs(**{key: {}}))


@pytest.mark.parametrize("key", ["ALERT_ROUTING", "ALERT_SECRETS"])
def test_multiline_values_and_bad_names_fail_without_printing_the_value(key):
    for bad in ({"A": "line1\nline2"}, {"A": "x\r"}, {"A B": "x"}, {"A=B": "x"}):
        got = msgs(**{key: bad})
        assert got, bad
        assert not any("line1" in m or "line2" in m for m in got)


# --- the script path rule ------------------------------------------------------------

def test_relative_script_src_resolves_against_the_vault_file_directory():
    f = facts(SKALERT_SCRIPT_SRC="sk-alert", vault_file_path="/etc/vaults/optional/skmon-prod_vault.yml")
    assert f["skmon_skalert_src"] == "/etc/vaults/optional/sk-alert"
    assert facts(SKALERT_SCRIPT_SRC="scripts/sk-alert", vault_file_path="/v/x.yml")["skmon_skalert_src"] == "/v/scripts/sk-alert"


def test_home_relative_script_src_is_expanded(monkeypatch):
    monkeypatch.setenv("HOME", "/home/fake")
    assert facts(SKALERT_SCRIPT_SRC="~/bin/sk-alert", vault_file_path="/v/x.yml")["skmon_skalert_src"] == "/home/fake/bin/sk-alert"


def test_absolute_script_src_is_used_as_given():
    assert facts(SKALERT_SCRIPT_SRC="/etc/estate/sk-alert", vault_file_path="/v/x.yml")["skmon_skalert_src"] == "/etc/estate/sk-alert"
    assert facts(SKALERT_SCRIPT_SRC="", vault_file_path="/v/x.yml")["skmon_skalert_src"] == ""


def test_plays_do_not_read_the_repo_root():
    assert "skstacks_repo_root" not in "".join(p.read_text() for p in PLAYS) + (ROLE / "tasks/facts.yml").read_text()


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_vault_load_is_no_log(play):
    assert task_named(play, "Load vault file")["no_log"] is True


def test_the_fixture_stub_matches_the_base_pin():
    import hashlib
    assert hashlib.sha256(FAKE_SK_ALERT.read_bytes()).hexdigest() == BASE["SKALERT_SCRIPT_SHA256"]


# --- the play tasks ------------------------------------------------------------------

def test_guards_hash_the_script_on_the_controller():
    t = task_named(GUARDS, "Hash the sk-alert script on the controller")
    assert t["stat"]["checksum_algorithm"] == "sha256" and t["stat"]["path"] == "{{ skmon_skalert_src }}"
    assert t["delegate_to"] == "localhost" and t["become"] is False


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_plays_deliver_script_and_secrets(play):
    tasks = play_tasks(play)
    cfg = next(t for t in tasks if "skmon_configs" in str(t.get("set_fact", "")))
    assert "skmon_skalert_src" in str(cfg["set_fact"]["skmon_configs"]["sk-alert"])
    sec = next(t for t in tasks if "skmon_secrets" in str(t.get("set_fact", "")))
    assert sec["no_log"] is True
    write = next(t for t in tasks if t.get("copy") and "/secrets/" in str(t["copy"].get("dest", "")))
    assert write["no_log"] is True and write["copy"]["mode"] == "0600"
    secret_dir = next(i for t in tasks if t.get("loop") for i in t["loop"] if isinstance(i, dict) and str(i.get("path", "")).endswith("-{{ env }}/secrets"))
    assert secret_dir["mode"] == "0700"


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_every_task_touching_secrets_is_no_log(play):
    for t in play_tasks(play):
        if "ALERT_SECRETS" in str(t) or "skmon_secrets" in str(t) or "alert.secret" in str(t):
            assert t.get("no_log") is True, t.get("name")


# --- the bridge service --------------------------------------------------------------

def test_bridge_service_shape():
    b = services()["bridge"]
    assert b["ports"] == [{"target": 9095, "published": 9095, "mode": "host", "protocol": "tcp"}]
    assert {s["source"] for s in b["secrets"]} == {"alert_env", "alert_secret"}
    assert not any(l.startswith("traefik.") and l != "traefik.enable=false" for l in b["deploy"].get("labels", []))
    assert b["deploy"]["resources"]["limits"]["memory"] == "64M"
    assert b["command"] == ["python3", "/opt/skmon/skmon_bridge.py"]
    assert b["networks"] == ["skmon-prod"]
    assert b["user"] == f"{UID}:{UID}"
    assert b["volumes"] == ["/srv/local/skmon-prod/bridge:/state"]
    assert b["image"].startswith("python:3.13.") and "@sha256:" in b["image"]


def test_bridge_mounts_script_and_secret_files():
    b = services()["bridge"]
    assert {"source": "skmon_bridge.py", "target": "/opt/skmon/skmon_bridge.py"} in b["configs"]
    assert {"source": "sk-alert", "target": "/opt/sk-alert/sk-alert", "mode": 0o555} in b["configs"]
    by_target = {s["target"]: s for s in b["secrets"]}
    assert set(by_target) == {"/opt/sk-alert/config/alert.env", "/opt/sk-alert/config/alert.secret"}
    for s in by_target.values():
        assert (s["uid"], s["gid"], s["mode"]) == (str(UID), str(UID), 0o400)


def test_bridge_uid_knob():
    b = services(BRIDGE_UID=10042)["bridge"]
    assert b["user"] == "10042:10042" and all(s["uid"] == "10042" for s in b["secrets"])


def test_bridge_environment():
    e = dict(kv.split("=", 1) for kv in env_of(services()["bridge"]))
    assert e == {
        "SKMON_ESTATE": "example", "SKMON_GRAFANA_URL": "https://skmon.c1.example.test",
        "SKMON_AM_URL": "http://skmon-prod_alertmanager:9093", "SKMON_SKALERT": "/opt/sk-alert/sk-alert",
        "SKMON_TALK": "0", "SKMON_HEALTHZ_PORT": "9095", "SKCAPSTONE_HOME": "/opt/sk-alert",
        "SKALERT_STATE_DIR": "/state", "SKALERT_LOG": "/state/skalert.log", "SKALERT_ENV_FILE": "/dev/null",
    }


def test_bridge_urls_follow_the_environment_suffix():
    dev = yaml.safe_load(render(CFG / "skmon.yml.j2", env="dev", extra_ctx=ctx_with_configs(env="dev")))["services"]["bridge"]
    e = dict(kv.split("=", 1) for kv in env_of(dev))
    assert e["SKMON_GRAFANA_URL"] == "https://skmon-dev.c1.example.test"
    assert e["SKMON_AM_URL"] == "http://skmon-dev_alertmanager:9093" and dev["networks"] == ["skmon-dev"]


def test_healthz_port_knob_sets_published_target_and_env_together():
    b = services(HEALTHZ_PORT=9199)["bridge"]
    assert b["ports"] == [{"target": 9199, "published": 9199, "mode": "host", "protocol": "tcp"}]
    assert "SKMON_HEALTHZ_PORT=9199" in env_of(b)


@pytest.mark.parametrize("v,want", [(True, "SKMON_TALK=1"), ("true", "SKMON_TALK=1"), (False, "SKMON_TALK=0"), ("false", "SKMON_TALK=0")])
def test_talk_flag_follows_vault(v, want):
    assert want in env_of(services(ALERT_TALK=v)["bridge"])


def test_secrets_not_in_argv_or_env():
    doc = compose(ALERT_SECRETS={"TELEGRAM_TOKEN": "SECRET-MARKER"}, ALERT_ROUTING={"CHAT": "ROUTING-MARKER"})
    assert "SECRET-MARKER" not in json.dumps(doc) and "ROUTING-MARKER" not in json.dumps(doc)


def test_sk_alert_not_vendored():
    assert not list(ROLE.rglob("sk-alert*"))


def test_secrets_are_hash_named_and_roll_the_bridge_on_change():
    a, b = compose()["secrets"], compose(ALERT_SECRETS={"TELEGRAM_BOT_TOKEN": "another-fake"})["secrets"]
    assert set(a) == {"alert_env", "alert_secret", "pve_config"}
    assert all(re.fullmatch(r"skmon_prod_alert_(env|secret)_[0-9a-f]{12}", c["name"]) for c in a.values())
    assert a["alert_env"]["file"] == "/var/data/config/skmon-prod/secrets/alert_env"
    assert a["alert_env"]["name"] == b["alert_env"]["name"] and a["alert_secret"]["name"] != b["alert_secret"]["name"]


def test_sk_alert_config_is_hash_named_from_its_content():
    c = compose()["configs"]
    assert re.fullmatch(r"skmon_prod_sk-alert_[0-9a-f]{12}", c["sk-alert"]["name"])
    assert c["sk-alert"]["file"] == "/var/data/config/skmon-prod/configs/sk-alert"
    assert c["skmon_bridge.py"]["file"] == "/var/data/config/skmon-prod/configs/skmon_bridge.py"


@pytest.mark.parametrize("r", [0, "0"])
def test_parked_bridge_still_renders_a_valid_compose(r):
    doc = compose(replicas=r)
    assert doc["services"]["bridge"]["deploy"]["replicas"] == 0 and "secrets" in doc


# --- secret files --------------------------------------------------------------------

NASTY = {"URL": "https://x.example.test/?a=1&b=2", "DOLLAR": "ab$cd#ef", "QUOTES": "it's \"q\" `x`",
         "SPACE": "two words;echo pwned", "BACK": "a\\b $(id)", "PLAIN": "2"}


@pytest.mark.parametrize("tpl,key", [("alert_env", "ALERT_ROUTING"), ("alert_secret", "ALERT_SECRETS")])
def test_secret_files_survive_bash_source(tmp_path, tpl, key):
    """sk-alert loads both files with bash `source`: every value must round-trip exactly."""
    import subprocess
    f = tmp_path / "f.env"
    f.write_text(render(CFG / SECRET_TEMPLATES[tpl], **{key: NASTY}))
    names = " ".join(NASTY)
    script = f'set -a; source "$1"; for n in {names}; do printf "%s=%s\\0" "$n" "${{!n}}"; done'
    out = subprocess.run(["bash", "-c", script, "bash", str(f)], capture_output=True, text=True, check=True,
                         cwd=tmp_path)
    got = dict(kv.split("=", 1) for kv in out.stdout.split("\0") if kv)
    assert got == NASTY
    assert not (tmp_path / "pwned").exists() and "pwned" not in out.stderr.replace("two words;echo pwned", "")


def test_alert_env_and_secret_render_one_pair_per_line():
    env = render(CFG / SECRET_TEMPLATES["alert_env"], ALERT_ROUTING={"B": "2", "A": "https://x.example.test/a=b"})
    assert env == "A=https://x.example.test/a=b\nB=2\n"
    sec = render(CFG / SECRET_TEMPLATES["alert_secret"], ALERT_SECRETS={"TOKEN": "fake"})
    assert sec == "TOKEN=fake\n"


# --- scrape and port -----------------------------------------------------------------

def test_bridge_is_scraped():
    cfgs = yaml.safe_load(render(CFG / "vm-scrape.yml.j2", extra_ctx={"skmon_agent_targets": []}))["scrape_configs"]
    job = next(j for j in cfgs if j["job_name"] == "bridge")
    assert job["scrape_interval"] == "30s"
    assert job["static_configs"] == [{"targets": ["skmon-prod_bridge:8080"], "labels": {"estate": "example"}}]
    dev = yaml.safe_load(render(CFG / "vm-scrape.yml.j2", env="dev", extra_ctx={"skmon_agent_targets": []}))["scrape_configs"]
    assert next(j for j in dev if j["job_name"] == "bridge")["static_configs"][0]["targets"] == ["skmon-dev_bridge:8080"]
