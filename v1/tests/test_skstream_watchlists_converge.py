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
    cd = cfg["cli_debrid"]
    assert {k: cd[k] for k in ("enabled", "branch_enabled", "release_version_enabled", "release_version")} == {
        "enabled": True, "branch_enabled": False, "release_version_enabled": True, "release_version": "v0.7.81"}
    assert not sc.converge_dumb(cfg, "v0.7.81")


def test_a_pin_points_cli_debrid_at_the_real_data_paths():
    # A pinned release is reinstalled into /cli_debrid on every start and the
    # /cli_debrid/data symlink did not survive it on nor (PermissionError).
    cfg = {"cli_debrid": {"config_file": "/cli_debrid/data/config/config.json",
                          "env": {"USER_CONFIG": "/cli_debrid/data/config/", "CLI_DEBRID_PORT": "5000"}}}
    assert sc.converge_dumb(cfg, "v0.7.81")
    cd = cfg["cli_debrid"]
    assert cd["config_file"] == "/data/cli_debrid/config/config.json"
    assert cd["log_file"] == "/data/cli_debrid/logs/debug.log"
    assert cd["env"] == {"USER_CONFIG": "/data/cli_debrid/config/", "USER_LOGS": "/data/cli_debrid/logs/",
                         "USER_DB_CONTENT": "/data/cli_debrid/db_content/", "CLI_DEBRID_PORT": "5000"}
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
    sc.converge_dumb(cfg, "v0.7.81")
    cfg["cli_battery"]["enabled"] = True
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
    for knob in ("CLI_DEBRID_VERSION", "PLEX_OTHER_WATCHLISTS", "PLEX_FRIENDS_WATCHLIST", "CLI_DEBRID_FILTER_OUT",
                 "CLI_DEBRID_FOREIGN_LANGUAGE_PENALTY", "CLI_DEBRID_MINIMUM_SCRAPE_SCORE"):
        assert knob in text, knob


# ---- filter_out (foreign-dub releases) --------------------------------------------

def _fo(cfg, v="1080p"):
    return cfg["Scraping"]["versions"][v]["filter_out"]


def test_null_filter_out_adds_the_default_russian_filter():
    cfg = seed_cli()
    assert sc.converge_cli(cfg, {"versions": ["1080p"], "filter_out": None})
    assert _fo(cfg) == sc.DEFAULT_FILTER_OUT
    assert not sc.converge_cli(cfg, {"versions": ["1080p"], "filter_out": None})


def test_filter_out_is_append_only_and_keeps_ui_patterns():
    cfg = seed_cli()
    cfg["Scraping"] = {"versions": {"1080p": {"filter_out": ["CAM", {"pattern": "x", "source": "nzb"}],
                                             "language_code": "en"}}}
    assert sc.converge_cli(cfg, {"versions": ["1080p"], "filter_out": ["x", "TS"]})
    assert _fo(cfg) == ["CAM", {"pattern": "x", "source": "nzb"}, "TS"]
    assert cfg["Scraping"]["versions"]["1080p"]["language_code"] == "en"


def test_empty_filter_out_list_adds_nothing():
    cfg = seed_cli()
    assert not sc.converge_cli(cfg, {"versions": ["1080p"], "filter_out": []})
    assert "Scraping" not in cfg


@pytest.mark.parametrize("title,hit", [
    ("The.Rainmaker.S02.1080p.Ru.Ultradox", True),
    ("Благодетель / The Rainmaker [02x01-02 из 10] (2026) WEB-DL 1080p", True),
    ("The.Rainmaker.S02E01-E02.HD1080p.WEBRip.Rus", True),
    ("The.Umbrella.Academy.LF_[rutor]", True),
    ("Will.Trent.S01.1080p.ViruseProject", True),
    ("Kill.2023.2160p.MegaPeer.mkv", True),
    ("The Rainmaker S02E02 Cards Up 1080p AMZN WEB-DL DDP5 1 H 264-NTb", False),
    ("RuPauls Drag Race S17E01 1080p", False),
    ("Rush 2013 1080p BluRay", False),
    ("True Detective S01 1080p", False),
    ("Peru Travel Doc 1080p", False),
])
def test_default_filter_matches_russian_releases_only(title, hit):
    import re
    # cli_debrid smart_search: re.search(pattern, text, re.IGNORECASE)
    assert bool(re.search(sc.DEFAULT_FILTER_OUT[0], title, re.IGNORECASE)) is hit


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_playbooks_pass_the_filter_knob(env):
    assert "'filter_out': skstream.CLI_DEBRID_FILTER_OUT | default(None)" in PLAYBOOKS[env].read_text()


# ---- foreign_language_penalty + minimum_scrape_score (drop, not rank down) --------

def test_null_score_knobs_set_the_defaults_and_rerun_is_a_no_op():
    cfg = seed_cli()
    spec = {"versions": ["1080p"], "foreign_language_penalty": None, "minimum_scrape_score": None}
    assert sc.converge_cli(cfg, spec)
    assert cfg["Scraping"]["versions"]["1080p"]["foreign_language_penalty"] == sc.DEFAULT_FOREIGN_LANGUAGE_PENALTY
    assert cfg["Scraping"]["minimum_scrape_score"] == sc.DEFAULT_MINIMUM_SCRAPE_SCORE
    assert not sc.converge_cli(cfg, spec)


def test_explicit_score_knobs_win_and_leave_other_version_keys_alone():
    cfg = seed_cli()
    cfg["Scraping"] = {"minimum_scrape_score": 0.0,
                       "versions": {"1080p": {"foreign_language_penalty": 30, "language_code": "en"}}}
    assert sc.converge_cli(cfg, {"versions": ["1080p"], "foreign_language_penalty": 500,
                                 "minimum_scrape_score": 0.0})
    v = cfg["Scraping"]["versions"]["1080p"]
    assert v == {"foreign_language_penalty": 500, "language_code": "en"}
    assert cfg["Scraping"]["minimum_scrape_score"] == 0.0


def test_absent_score_knobs_change_nothing():
    cfg = seed_cli()
    cfg["Scraping"] = {"minimum_scrape_score": 0.0, "versions": {"1080p": {"foreign_language_penalty": 30}}}
    assert not sc.converge_cli(cfg, {"versions": ["1080p"]})
    assert cfg["Scraping"] == {"minimum_scrape_score": 0.0, "versions": {"1080p": {"foreign_language_penalty": 30}}}


def test_an_equal_int_and_float_is_not_a_change():
    # nor's live config carries 1000 (int) and 1.0; a deploy must not rewrite it.
    cfg = seed_cli()
    cfg["Scraping"] = {"minimum_scrape_score": 1, "versions": {"1080p": {"foreign_language_penalty": 1000}}}
    assert not sc.converge_cli(cfg, {"versions": ["1080p"], "foreign_language_penalty": None,
                                     "minimum_scrape_score": None})


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_playbooks_pass_the_score_knobs(env):
    text = PLAYBOOKS[env].read_text()
    assert "'foreign_language_penalty': skstream.CLI_DEBRID_FOREIGN_LANGUAGE_PENALTY | default(None)" in text
    assert "'minimum_scrape_score': skstream.CLI_DEBRID_MINIMUM_SCRAPE_SCORE | default(None)" in text


# ---- 3D / disc-image junk and multi-audio ranking (nor, 2026-11-15) ---------------

@pytest.mark.parametrize("title,hit", [
    ("Ant-Man.2015.1080p.3D.BluRay.AVC.DTS-HD.MA.7.1-RARBG", True),
    ("Captain.America.The.Winter.Soldier.2014.1080p.3D.BluRay.AVC.DTS-HD.MA.7.1-RARBG", True),
    ("RARBG.com.mp4", True),
    ("Pulp.Fiction.1994.1080p.3D.FULL-SBS.HEVC.ENG-HUN", True),
    ("Avatar.2009.COMPLETE.BLURAY-GROUP", True),
    ("Movie.2010.1080p.BluRay.BDMV", True),
    ("Piranha.3D.2010.1080p.BluRay.x264", False),
    ("Jackass.3D.2010.1080p.WEB-DL", False),
    ("Ant-Man.2015.1080p.BluRay.x264-SPARKS", False),
    ("Ant-Man.2015.1080p.BluRay.REMUX.AVC.DTS-HD.MA.7.1-RARBG", False),
])
def test_default_filter_drops_3d_disc_images_and_the_rarbg_promo(title, hit):
    import re
    assert bool(re.search(sc.DEFAULT_FILTER_OUT[1], title, re.IGNORECASE)) is hit


@pytest.mark.parametrize("title,hit", [
    ("Stranger.Things.s01e01.2016.1080p.Rifftrax.6ch.2ch.AVC.mkv", True),
    ("Twilight.2008.RiffTrax.720p", True),
    ("RiffTrax Live Manos The Hands of Fate", True),
    ("Riff-Trax.Plan.9.From.Outer.Space", True),
    ("Stranger Things (2016) S01 Complete (1080p BluRay AV1 10bit English Opus 5.1) [SkyLake]", False),
    ("Sheriff.Country.S01E01.1080p.WEB.h264-GRACE", False),
    ("Riffraff.2019.1080p.BluRay", False),
])
def test_default_filter_drops_rifftrax(title, hit):
    import re
    assert bool(re.search(sc.DEFAULT_FILTER_OUT[2], title, re.IGNORECASE)) is hit


@pytest.mark.parametrize("title,hit", [
    ("You.Can.See.Everything.2026.AUDIONEW.1080P", True),
    ("Movie.2025.1080p.HDTS.NEW.AUDIO.x264", True),
    ("Film.2024.NewAudio.720p", True),
    ("You.Can.See.Everything.2026.REPACK2.1080p.AMZN.WEB-DL.DDP5.1.Atmos.H.264-CHORTLE", False),
    ("Brave.New.World.S01.1080p.WEB-DL", False),
    ("Audio.Newsreel.1960.1080p", False),
    ("The.Marksman.HDCAM.850MB.c1nem4.x264-SUNSCREEN", True),
    ("John.Wick.Chapter.4.2023.1080p.HQCAM.Hindi-English.x264.1XBET", True),
    ("Venom.Let.There.Be.Carnage.2021.720p.CAMRip.English.x264-1XBET", True),
    ("Barbie 2023 720p HDTS X264 AC3-AOC", True),
    ("Beetlejuice.Beetlejuice.2024.REPACK.1080p.TELESYNC.x264.COLLECTiVE", True),
    ("Mufasa The Lion King (2024) 1080p DVDScr - x264 - AAC - 2GB", True),
    ("Spider.Man.No.Way.Home.2021.1080p.HD-TS.LiNE.V3-C1NEM4", True),
    ("Cam.2018.1080p.NF.WEB-DL.DDP5.1.x264", False),
    ("Webcam.2023.1080p.WEB-DL", False),
    ("The.Screening.2024.1080p.BluRay", False),
    ("Se7en 1995 1080p.ts", False),
])
def test_default_filter_drops_audionew_cam_rips(title, hit):
    import re
    assert bool(re.search(sc.DEFAULT_FILTER_OUT[3], title, re.IGNORECASE)) is hit


@pytest.mark.parametrize("title,hit", [
    ("Heart.of.the.Beast.2026.MULTi.1080p.WEB.H265-YTO", True),
    ("Ava.2020.iTA-ENG.Bluray.1080p.x264-CYBER", True),
    ("John.Wick.2014.MULTi4.2160p.HDR.BluRay.AV1-Kleinmike", True),
    ("Knowing (2009) 1080p BluRay x264 {Dual Audio} {Hindi DD+2.0-English BD 5.1}", True),
    ("Deadpool.and.Wolverine.2024.MULTi.VF2.HDR.2160p.WEB.H265", True),
    ("Fast.X.2023.1080p.REMUX.ENG.Multi.Sub", False),
    ("Fast.X.2023.1080p.REMUX.Multi-Subs", False),
    ("Heart.of.the.Beast.2026.1080p.WEB-DL.DDP5.1.H.264-FLUX", False),
])
def test_default_preferred_filter_out_ranks_down_multi_audio(title, hit):
    import re
    pat, weight = sc.DEFAULT_PREFERRED_FILTER_OUT[0]
    assert weight == 400
    assert bool(re.search(pat, title, re.IGNORECASE)) is hit


def _pfo(cfg, v="1080p"):
    return cfg["Scraping"]["versions"][v]["preferred_filter_out"]


def test_null_preferred_filter_out_adds_the_default_and_rerun_is_a_no_op():
    cfg = seed_cli()
    assert sc.converge_cli(cfg, {"versions": ["1080p"], "preferred_filter_out": None})
    assert _pfo(cfg) == sc.DEFAULT_PREFERRED_FILTER_OUT
    assert not sc.converge_cli(cfg, {"versions": ["1080p"], "preferred_filter_out": None})


def test_preferred_filter_out_upserts_by_pattern_and_keeps_ui_pairs():
    cfg = seed_cli()
    cfg["Scraping"] = {"versions": {"1080p": {"preferred_filter_out": [["CAM", 5000], ["x", 10]]}}}
    assert sc.converge_cli(cfg, {"versions": ["1080p"], "preferred_filter_out": [["x", 400.0], ["y", 7]]})
    assert _pfo(cfg) == [["CAM", 5000], ["x", 400], ["y", 7]]


def test_absent_or_empty_preferred_filter_out_changes_nothing():
    cfg = seed_cli()
    assert not sc.converge_cli(cfg, {"versions": ["1080p"]})
    assert not sc.converge_cli(cfg, {"versions": ["1080p"], "preferred_filter_out": []})


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_playbooks_pass_the_preferred_filter_knob(env):
    text = PLAYBOOKS[env].read_text()
    assert "'preferred_filter_out': skstream.CLI_DEBRID_PREFERRED_FILTER_OUT | default(None)" in text


# ---- Reverse Parser default version (version-less rows skip every filter) ---------

def test_null_reverse_parser_default_is_the_first_managed_version():
    cfg = seed_cli()
    cfg["Reverse Parser"] = {"version_terms": {}, "default_version": ""}
    assert sc.converge_cli(cfg, {"versions": ["1080p"], "reverse_parser_default_version": None})
    assert cfg["Reverse Parser"] == {"version_terms": {}, "default_version": "1080p"}
    assert not sc.converge_cli(cfg, {"versions": ["1080p"], "reverse_parser_default_version": None})


def test_absent_reverse_parser_knob_changes_nothing():
    cfg = seed_cli()
    cfg["Reverse Parser"] = {"default_version": ""}
    assert not sc.converge_cli(cfg, {"versions": ["1080p"]})
    assert cfg["Reverse Parser"]["default_version"] == ""


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_playbooks_pass_the_reverse_parser_knob(env):
    text = PLAYBOOKS[env].read_text()
    assert ("'reverse_parser_default_version': skstream.CLI_DEBRID_REVERSE_PARSER_DEFAULT_VERSION | default(None)"
            in text)
