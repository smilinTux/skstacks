"""skstream: cli_debrid release pin and Plex watchlist sources as code.

Prod (nor, 2026-11) ran six "Other Plex Watchlist" sources (managed Plex Home
profiles), a friends RSS source and a hand-patched plex_watchlist.py, none of
them in the repo: the DUMB and cli_debrid configs are seeded once and then
owned by the apps, so a deploy could neither reproduce them nor carry a new
viewer. `skstream-converge` upserts only what skstream owns, and the play pins
cli_debrid to the release that carries the managed Home fix (de5c411f), which
retires the patch.
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import stat

import pytest
import yaml

APP = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skstream"
SCRIPT = APP / "src/skstream/skstream-converge"
README = APP / "README.md"
PLAYBOOKS = {e: APP / f"deploy_skstream-{e}.yml" for e in ("dev", "staging", "prod")}
PATCH_TARGET = "/cli_debrid/content_checkers/plex_watchlist.py"


def _load():
    loader = importlib.machinery.SourceFileLoader("skstream_converge", str(SCRIPT))
    spec = importlib.util.spec_from_loader("skstream_converge", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


sc = _load()


def seed_cli():
    return {"Plex": {"token": "owner"}, "Debug": {"auto_run_program": True,
            "content_source_check_period": {"My Plex Watchlist_1": 5}},
            "Content Sources": {"My Plex Watchlist_1": {"enabled": True, "type": "My Plex Watchlist"}}}


SPEC = {"other_watchlists": [{"username": "JD", "token": "tok-jd"}],
        "friends_rss_url": "https://rss.plex.tv/friends", "versions": ["1080p"],
        "cutoff_date": "", "check_minutes": 5}


# ---- converge_cli ---------------------------------------------------------------

def test_adds_one_source_per_home_profile_and_one_friends_feed():
    cfg = seed_cli()
    assert sc.converge_cli(cfg, SPEC)
    cs = cfg["Content Sources"]
    jd = [v for v in cs.values() if v.get("username") == "JD"]
    fr = [v for v in cs.values() if v.get("type") == "My Friends Plex RSS Watchlist"]
    assert len(jd) == 1 and jd[0]["token"] == "tok-jd" and jd[0]["type"] == "Other Plex Watchlist"
    assert jd[0]["display_name"] == "Plex Watchlist - JD" and jd[0]["versions"] == ["1080p"]
    assert len(fr) == 1 and fr[0]["url"] == "https://rss.plex.tv/friends"
    assert set(cs) == {"My Plex Watchlist_1", "Other Plex Watchlist_2", "My Friends Plex RSS Watchlist_3"}


def test_second_run_is_a_no_op():
    cfg = seed_cli()
    sc.converge_cli(cfg, SPEC)
    before = json.dumps(cfg, sort_keys=True)
    assert not sc.converge_cli(cfg, SPEC)
    assert json.dumps(cfg, sort_keys=True) == before


def test_check_period_applies_to_managed_sources_only():
    cfg = seed_cli()
    cfg["Content Sources"]["MDBList_7"] = {"type": "MDBList"}
    sc.converge_cli(cfg, dict(SPEC, check_minutes=3))
    period = cfg["Debug"]["content_source_check_period"]
    assert period["Other Plex Watchlist_8"] == 3 and period["My Friends Plex RSS Watchlist_9"] == 3
    assert period["My Plex Watchlist_1"] == 5 and "MDBList_7" not in period


def test_matches_an_existing_source_by_username_and_updates_only_the_token():
    cfg = seed_cli()
    cfg["Content Sources"]["Other Plex Watchlist_4"] = {
        "enabled": True, "type": "Other Plex Watchlist", "username": "JD", "token": "old",
        "display_name": "kept as the UI left it", "versions": ["4k"]}
    assert sc.converge_cli(cfg, dict(SPEC, friends_rss_url=""))
    jd = cfg["Content Sources"]["Other Plex Watchlist_4"]
    assert jd["token"] == "tok-jd" and jd["display_name"] == "kept as the UI left it" and jd["versions"] == ["4k"]
    assert sum(v.get("username") == "JD" for v in cfg["Content Sources"].values()) == 1


def test_re_enables_a_managed_source_switched_off():
    cfg = seed_cli()
    sc.converge_cli(cfg, SPEC)
    cfg["Content Sources"]["Other Plex Watchlist_2"]["enabled"] = False
    assert sc.converge_cli(cfg, SPEC)
    assert cfg["Content Sources"]["Other Plex Watchlist_2"]["enabled"] is True


def test_never_removes_sources_it_did_not_create():
    cfg = seed_cli()
    cfg["Content Sources"]["Other Plex Watchlist_5"] = {"type": "Other Plex Watchlist", "username": "Wisk", "token": "w"}
    sc.converge_cli(cfg, {"other_watchlists": [], "friends_rss_url": ""})
    assert "Other Plex Watchlist_5" in cfg["Content Sources"] and "My Plex Watchlist_1" in cfg["Content Sources"]


def test_empty_spec_changes_nothing():
    cfg = seed_cli()
    before = json.dumps(cfg, sort_keys=True)
    assert not sc.converge_cli(cfg, {})
    assert json.dumps(cfg, sort_keys=True) == before


# ---- converge_dumb --------------------------------------------------------------

def test_pins_cli_debrid_and_turns_branch_off():
    cfg = {"cli_debrid": {"enabled": True, "branch_enabled": True, "release_version": "v0.6.07"}}
    assert sc.converge_dumb(cfg, "v0.7.81")
    assert cfg["cli_debrid"] == {"enabled": True, "branch_enabled": False,
                                 "release_version_enabled": True, "release_version": "v0.7.81"}
    assert not sc.converge_dumb(cfg, "v0.7.81")


def test_empty_version_leaves_dumb_alone():
    cfg = {"cli_debrid": {"enabled": True}, "cli_battery": {"enabled": True}}
    assert not sc.converge_dumb(cfg, "")
    assert cfg == {"cli_debrid": {"enabled": True}, "cli_battery": {"enabled": True}}


def test_a_pin_switches_off_the_standalone_battery():
    # cli_debrid >= v0.7.29 has no cli_battery/main.py; DUMB's own battery service
    # then crashes and holds cli_debrid back (seen on nor 2026-11-08).
    cfg = {"cli_debrid": {"enabled": True, "release_version_enabled": True,
                          "release_version": "v0.7.81", "branch_enabled": False},
           "cli_battery": {"enabled": True, "process_name": "CLI Battery"}}
    assert sc.converge_dumb(cfg, "v0.7.81")
    assert cfg["cli_battery"] == {"enabled": False, "process_name": "CLI Battery"}
    assert not sc.converge_dumb(cfg, "v0.7.81")


def test_no_battery_section_is_not_invented():
    cfg = {"cli_debrid": {}}
    sc.converge_dumb(cfg, "v0.7.81")
    assert "cli_battery" not in cfg


# ---- main: files, ownership, output ---------------------------------------------

def _files(tmp_path):
    d, c = tmp_path / "dumb_config.json", tmp_path / "config.json"
    d.write_text(json.dumps({"cli_debrid": {"enabled": True}, "zurg": {"x": 1}}))
    c.write_text(json.dumps(seed_cli()))
    os.chmod(d, 0o600)
    os.chmod(c, 0o600)
    (tmp_path / "config.lock").write_text("")
    return d, c


def _run(monkeypatch, capsys, d, c, spec):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(spec)))
    sc.main(["--dumb-config", str(d), "--cli-config", str(c)])
    return json.loads(capsys.readouterr().out)


def test_main_reports_changes_keeps_mode_and_prints_no_secret(tmp_path, monkeypatch, capsys):
    d, c = _files(tmp_path)
    out = _run(monkeypatch, capsys, d, c, dict(SPEC, cli_debrid_version="v0.7.81"))
    assert out == {"dumb_changed": True, "cli_changed": True}
    assert "tok-jd" not in json.dumps(out) and "rss.plex.tv" not in json.dumps(out)
    for f in (d, c):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert json.loads(d.read_text())["zurg"] == {"x": 1}
    assert not list(tmp_path.glob("*.tmp"))
    assert _run(monkeypatch, capsys, d, c, dict(SPEC, cli_debrid_version="v0.7.81")) == {
        "dumb_changed": False, "cli_changed": False}


def test_main_leaves_an_unchanged_file_untouched(tmp_path, monkeypatch, capsys):
    d, c = _files(tmp_path)
    mtime = c.stat().st_mtime_ns
    _run(monkeypatch, capsys, d, c, {"cli_debrid_version": ""})
    assert c.stat().st_mtime_ns == mtime


def test_main_fails_on_a_corrupt_config_without_rewriting_it(tmp_path, monkeypatch):
    d, c = _files(tmp_path)
    c.write_text("{not json")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SPEC)))
    with pytest.raises(json.JSONDecodeError):
        sc.main(["--dumb-config", str(d), "--cli-config", str(c)])
    assert c.read_text() == "{not json"


# ---- playbooks ------------------------------------------------------------------

def tasks(env):
    out = []
    for play in yaml.safe_load(PLAYBOOKS[env].read_text()):
        for sec in ("pre_tasks", "tasks"):
            out += play.get(sec) or []
    return out


def named(env, needle):
    found = [t for t in tasks(env) if needle in t.get("name", "")]
    assert len(found) == 1, (env, needle)
    return found[0]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_converge_runs_on_the_media_node_with_secrets_on_stdin(env):
    t = named(env, "Converge the cli_debrid pin")
    assert t["delegate_to"] == "{{ skstream.MEDIA_NODE }}" and t["no_log"] is True
    assert "PLEX_OTHER_WATCHLISTS" in t["command"]["stdin"]
    assert "TOKEN" not in json.dumps(t["command"]["argv"])
    assert "v0.7.81" in t["command"]["stdin"]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_friends_feed_is_opt_in_and_no_log(env):
    t = named(env, "friends-watchlist RSS")
    assert t["no_log"] is True and "PLEX_FRIENDS_WATCHLIST" in t["when"] and "default(false)" in t["when"]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_converge_runs_before_dumb_starts(env):
    names = [t.get("name", "") for t in tasks(env)]
    conv = next(i for i, n in enumerate(names) if n.startswith("Converge the cli_debrid pin"))
    start = next(i for i, n in enumerate(names) if n.startswith("Enable and start skstream-dumb.service"))
    assert conv < start


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_a_new_pin_restarts_dumb_and_sources_alone_restart_cli_debrid_only(env):
    start = named(env, "Enable and start skstream-dumb.service")
    assert "dumb_changed" in start["systemd"]["state"] and "skstream_unit.changed" in start["systemd"]["state"]
    only = named(env, "Restart only cli_debrid")
    assert "restart-service" in only["command"]["argv"][-1] or "restart-service" in json.dumps(only["command"]["argv"])
    assert any("cli_changed" in w for w in only["when"]) and any("skstream_dumb_start.changed" in w for w in only["when"])


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_play_refuses_the_old_watchlist_patch_over_a_pinned_release(env):
    t = named(env, "Refuse a plex_watchlist.py bind")
    assert PATCH_TARGET in json.dumps(t["assert"]["that"])
    assert "CLI_DEBRID_VERSION" in json.dumps(t["assert"]["that"])


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_other_watchlists_are_validated(env):
    t = named(env, "Validate skstream.PLEX_OTHER_WATCHLISTS")
    assert "PLEX_OTHER_WATCHLISTS" in t["loop"]


def test_readme_documents_the_knobs():
    text = README.read_text()
    for knob in ("CLI_DEBRID_VERSION", "PLEX_OTHER_WATCHLISTS", "PLEX_FRIENDS_WATCHLIST"):
        assert knob in text, knob
