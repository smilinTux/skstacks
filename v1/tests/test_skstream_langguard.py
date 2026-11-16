"""skstream-langguard: English audio for every Plex viewer, foreign dubs re-fetched.

nor (2026-11-15): release-name filters let through ~1150 files whose default
audio was not English (MULTi/iTA-ENG releases, untagged dubs), ~60 of them with
no English audio at all, and a friend account (no language preference of its
own on our side) got French on Heart of the Beast. The guard reads the audio
tracks Plex indexed, selects English per viewer where it exists, and resets
cli_debrid items that have neither English audio nor original-language audio
with English subtitles.
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import sqlite3
import stat
import subprocess
import xml.etree.ElementTree as ET

import pytest
import yaml

APP = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skstream"
SCRIPT = APP / "src/skstream/skstream-langguard"
WRAPPER = APP / "src/skstream/skstream-langguard-run"
UNITS = [APP / "src/config/skstream/skstream-langguard.service.j2",
         APP / "src/config/skstream/skstream-langguard.timer.j2"]
README = APP / "README.md"
PLAYBOOKS = {e: APP / f"deploy_skstream-{e}.yml" for e in ("dev", "staging", "prod")}


def _load():
    loader = importlib.machinery.SourceFileLoader("skstream_langguard", str(SCRIPT))
    spec = importlib.util.spec_from_loader("skstream_langguard", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


lg = _load()


# ---- the rule --------------------------------------------------------------------

@pytest.mark.parametrize("code,want", [
    ("eng", "en"), ("en-US", "en"), ("EN", "en"), ("ita", "it"), ("fre", "fr"), ("fra", "fr"),
    ("ger", "de"), ("kor", "ko"), ("chi", "zh"), ("cn", "zh"), ("pt-BR", "pt"), ("es-419", "es"),
    ("und", None), ("", None), (None, None),
])
def test_lang_normalizes_plex_and_tmdb_codes(code, want):
    assert lg.lang(code) == want


@pytest.mark.parametrize("audio,subs,original,verdict", [
    # English audio anywhere is fine; the select pass makes it the viewer's choice.
    (["fre", "fre", "por", "eng"], ["fre"], "en", "ok"),            # Heart of the Beast MULTi YTO
    (["ita", "eng"], [], None, "ok"),
    # Original language with English subtitles is fine.
    (["kor"], ["eng"], "ko", "ok"),                                  # Parasite
    (["jpn"], ["jpn", "eng"], "ja", "ok"),                           # Godzilla Minus One
    (["ita", "hin"], ["ita", "eng"], "hi", "ok"),                    # Kill: original present
    # Everything else with no English audio is not.
    (["spa"], [], "es", "bad"),                                      # The Platform, no subs
    (["fre"], [], "en", "bad"),                                      # Tulsa King FRENCH
    (["heb"], ["eng"], "en", "bad"),                                 # Minions HebDub
    (["hin"], ["eng"], "de", "bad"),                                 # Maxton Hall Hindi dub
    (["fre"], ["eng"], "it", "bad"),                                 # Supersex FRENCH
    # Not enough to decide: report, never delete.
    (["und"], [], "en", "unknown"),
    ([None], [], "en", "unknown"),
    ([], [], "en", "unknown"),
    (["kor"], ["eng"], None, "unknown"),
])
def test_judge(audio, subs, original, verdict):
    assert lg.judge(audio, subs, original)[0] == verdict


def _s(i, code, selected=False, title=None, kind="2"):
    e = ET.Element("Stream", id=str(i), streamType=kind, languageCode=code)
    if selected:
        e.set("selected", "1")
    if title:
        e.set("title", title)
    return e


def test_english_choice_picks_english_and_drops_foreign_forced_subs():
    audio = [_s(1, "fre", True), _s(2, "por"), _s(3, "eng", title="Commentary"), _s(4, "eng")]
    subs = [_s(9, "fre", True, "Forced", kind="3"), _s(10, "eng", kind="3")]
    assert lg.english_choice(audio, subs) == {"audioStreamID": "4", "subtitleStreamID": 0}


def test_english_choice_is_a_no_op_when_english_is_selected_or_absent():
    assert lg.english_choice([_s(1, "ita"), _s(2, "eng", True)], [_s(9, "eng", True, kind="3")]) == {}
    assert lg.english_choice([_s(1, "kor", True)], [_s(9, "eng", True, kind="3")]) == {}


# ---- cli_debrid side -------------------------------------------------------------

COLS = ("id INTEGER PRIMARY KEY, imdb_id TEXT, title TEXT, type TEXT, state TEXT, version TEXT, "
        "location_on_disk TEXT, original_path_for_symlink TEXT, filled_by_file TEXT, filled_by_title TEXT, "
        "filled_by_magnet TEXT, filled_by_torrent_id TEXT, debrid_folder_name TEXT, scrape_results TEXT, "
        "nzb_segment_id TEXT, real_debrid_original_title TEXT, location_basename TEXT, collected_at TEXT, "
        "original_collected_at TEXT, rescrape_original_torrent_title TEXT, original_scraped_torrent_title TEXT, "
        "upgrading_from TEXT, upgrading TEXT, fall_back_to_single_scraper INTEGER, last_updated TEXT")


def media_db(rows):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(f"CREATE TABLE media_items ({COLS})")
    for r in rows:
        db.execute(f"INSERT INTO media_items ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", tuple(r.values()))
    db.commit()
    return db


def battery_db(langs):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, imdb_id TEXT)")
    db.execute("CREATE TABLE metadata (id INTEGER PRIMARY KEY, item_id INTEGER, key TEXT, value TEXT)")
    for n, (imdb, code) in enumerate(langs.items(), 1):
        db.execute("INSERT INTO items VALUES (?, ?)", (n, imdb))
        db.execute("INSERT INTO metadata (item_id, key, value) VALUES (?, 'language', ?)", (n, json.dumps(code)))
    return db


def row(id, path, tid=None, version="1080p", imdb="tt1", title="T", state="Collected"):
    return {"id": id, "imdb_id": imdb, "title": title, "type": "movie", "state": state, "version": version,
            "location_on_disk": path, "filled_by_title": f"{title}.release", "filled_by_torrent_id": tid,
            "original_scraped_torrent_title": f"{title}.release"}


@pytest.mark.parametrize("version,want", [("1080p", "1080p"), ("1080p*", "1080p"), ("**", "1080p"),
                                          ("", "1080p"), (None, "1080p")])
def test_reset_item_mirrors_the_rescrape_route_and_never_leaves_a_blank_version(version, want):
    # A blank version makes cli_debrid scrape with default settings: no filters at all.
    db = media_db([row(1, "/m/a/a.mkv", "TID", version)])
    item = db.execute("SELECT * FROM media_items WHERE id=1").fetchone()
    assert lg.reset_item(db, item, "1080p")
    r = db.execute("SELECT * FROM media_items WHERE id=1").fetchone()
    assert (r["state"], r["version"], r["location_on_disk"], r["filled_by_torrent_id"]) == ("Wanted", want, None, None)
    assert r["rescrape_original_torrent_title"] == "T.release"  # cli_debrid skips that release next time


def test_reset_item_leaves_a_row_that_is_no_longer_collected():
    db = media_db([row(1, "/m/a/a.mkv", state="Wanted")])
    assert not lg.reset_item(db, db.execute("SELECT * FROM media_items").fetchone(), "1080p")


class FakeRD:
    def __init__(self, folders=None):
        self.deleted, self.folders = [], folders or {}

    def delete(self, tid):
        self.deleted.append(tid)
        return 204

    def by_folder(self, folder):
        return self.folders.get(folder, [])


def test_drop_torrent_keeps_a_pack_other_collected_items_still_use():
    db = media_db([row(1, "/m/Pack/e1.mkv", "PACK"), row(2, "/m/Pack/e2.mkv", "PACK")])
    rd = FakeRD()
    item = db.execute("SELECT * FROM media_items WHERE id=1").fetchone()
    assert lg.drop_torrent(db, rd, item, "/m/Pack/e1.mkv") == "kept (shared)" and rd.deleted == []
    db.execute("UPDATE media_items SET state='Wanted' WHERE id=2")
    assert lg.drop_torrent(db, rd, item, "/m/Pack/e1.mkv") == 204 and rd.deleted == ["PACK"]


def test_drop_torrent_without_an_id_matches_the_zurg_folder_and_respects_sharing():
    db = media_db([row(1, "/m/Folder/a.mkv"), row(2, "/m/Folder/b.mkv")])
    rd = FakeRD({"Folder": ["RD1"]})
    item = db.execute("SELECT * FROM media_items WHERE id=1").fetchone()
    assert lg.drop_torrent(db, rd, item, "/m/Folder/a.mkv") == "kept (shared)"
    db.execute("DELETE FROM media_items WHERE id=2")
    assert lg.drop_torrent(db, rd, item, "/m/Folder/a.mkv") == [204] and rd.deleted == ["RD1"]


# ---- run() against a fake Plex ---------------------------------------------------

class FakePlex:
    """items: {key: [(media, part, file, [(code, selected)], [(code, selected)])]}"""

    def __init__(self, items, users=("owner",), recent=None):
        self.lib, self.user_ids, self.recent = items, list(users), recent
        self.selected, self.deleted, self.asked_since = [], [], []

    def users(self):
        return {u: (u, f"tok-{u}") for u in self.user_ids}

    def items(self, since):
        self.asked_since.append(since)
        return list(self.recent if since and self.recent is not None else self.lib)

    def parts(self, key, token=None):
        out = []
        for media, part, path, audio, subs in self.lib[key]:
            a = [_s(n, c, sel) for n, (c, sel) in enumerate(audio, 1)]
            s = [_s(100 + n, c, sel, kind="3") for n, (c, sel) in enumerate(subs, 1)]
            out.append((media, part, path, a, s))
        return out

    def select(self, part, token, **q):
        self.selected.append((part, token, q))

    def delete_media(self, key, media):
        self.deleted.append((key, media))


LIB = {
    "10": [("m10", "p10", "/m/HOTB/hotb.mkv", [("fre", True), ("eng", False)], [("fre", True)])],
    "11": [("m11", "p11", "/m/Parasite/p.mkv", [("kor", True)], [("eng", False)])],
    "12": [("m12", "p12", "/m/Tulsa/t.mkv", [("fre", True)], [])],
    "13": [("m13", "p13", "/m/Other/o.mkv", [("spa", True)], [])],   # not a cli_debrid item
    "14": [("m14", "p14", "/m/Und/u.mkv", [("und", True)], [])],
}


def fixture():
    db = media_db([row(1, "/m/HOTB/hotb.mkv", "T1", imdb="tt10", title="HOTB"),
                   row(2, "/m/Parasite/p.mkv", "T2", imdb="tt11", title="Parasite"),
                   row(3, "/m/Tulsa/t.mkv", "T3", version="**", imdb="tt12", title="Tulsa"),
                   row(4, "/m/Und/u.mkv", "T4", imdb="tt14", title="Und")])
    bat = battery_db({"tt10": "en", "tt11": "ko", "tt12": "en", "tt14": "en"})
    return db, bat


def test_dry_run_reports_and_changes_nothing():
    db, bat = fixture()
    plex, rd, state, log = FakePlex(LIB), FakeRD(), {}, []
    s = lg.run(plex, db, bat, rd, state, apply=False, log=log.append)
    assert (s["bad"], s["reset"], s["unmanaged"], s["unknown"], s["selected"]) == (1, 0, 1, 1, 1)
    assert plex.selected == plex.deleted == rd.deleted == []
    assert db.execute("SELECT state FROM media_items WHERE id=3").fetchone()[0] == "Collected"
    assert any(m.startswith("WOULD RESET Tulsa") for m in log) and "last_run" not in state


def test_apply_resets_only_the_dub_selects_english_and_leaves_the_rest():
    db, bat = fixture()
    plex, rd, state = FakePlex(LIB, users=("owner", "casey")), FakeRD(), {}
    s = lg.run(plex, db, bat, rd, state, apply=True, now=1000, log=lambda m: None)
    assert s["reset"] == 1 and rd.deleted == ["T3"] and plex.deleted == [("12", "m12")]
    r = db.execute("SELECT state, version FROM media_items WHERE id=3").fetchone()
    assert tuple(r) == ("Wanted", "1080p")
    # Parasite (original + English subs), the untagged file and the unmanaged one stay.
    assert [x[0] for x in db.execute("SELECT state FROM media_items WHERE id IN (2, 4) ORDER BY id")] == ["Collected"] * 2
    # Every viewer gets English on the MULTi file, foreign forced subs off.
    assert sorted(plex.selected) == [("p10", "tok-casey", {"audioStreamID": "2", "subtitleStreamID": 0}),
                                     ("p10", "tok-owner", {"audioStreamID": "2", "subtitleStreamID": 0})]
    assert state["last_run"] == 1000 and state["tries"] == {"3": 1} and state["mixed"] == ["10"]


def test_max_resets_caps_one_run():
    db = media_db([row(i, f"/m/F{i}/f.mkv", f"T{i}", imdb="tt1") for i in range(1, 4)])
    lib = {str(i): [(f"m{i}", f"p{i}", f"/m/F{i}/f.mkv", [("fre", True)], [])] for i in range(1, 4)}
    s = lg.run(FakePlex(lib), db, battery_db({"tt1": "en"}), FakeRD(), {}, apply=True, max_resets=2,
               log=lambda m: None)
    assert s["reset"] == 2 and s["bad"] == 3


def test_an_item_reset_max_tries_times_is_left_and_reported():
    db, bat = fixture()
    state, log = {"tries": {"3": lg.MAX_TRIES}}, []
    s = lg.run(FakePlex(LIB), db, bat, FakeRD(), state, apply=True, log=log.append)
    assert s["reset"] == 0 and s["gave_up"] == 1 and any(m.startswith("GAVE UP Tulsa") for m in log)


def test_incremental_runs_and_a_new_viewer_gets_every_mixed_item():
    db, bat = fixture()
    state = {"last_run": 10_000, "users": ["owner"], "mixed": ["99"]}
    lib = dict(LIB, **{"99": [("m99", "p99", "/m/Old/o.mkv", [("ita", True), ("eng", False)], [])]})
    plex = FakePlex(lib, users=("owner", "newfriend"), recent=["10"])
    lg.run(plex, db, bat, FakeRD(), state, apply=True, log=lambda m: None)
    assert plex.asked_since == [10_000 - 3600]
    got = {(p, t) for p, t, _ in plex.selected}
    # the old mixed item only for the new viewer; the new one for everyone
    assert got == {("p99", "tok-newfriend"), ("p10", "tok-newfriend"), ("p10", "tok-owner")}
    assert state["users"] == ["newfriend", "owner"] and state["mixed"] == ["10", "99"]


# ---- wrapper, units, playbooks, docs ---------------------------------------------

def test_wrapper_passes_the_token_on_stdin_never_argv(tmp_path):
    prefs = tmp_path / "Preferences.xml"
    prefs.write_text('<?xml version="1.0"?><Preferences ProcessedMachineIdentifier="mid1" '
                     'PlexOnlineToken="sekret-token" FriendlyName="x"/>')
    fake = tmp_path / "docker"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$OUT.argv"\ncat > "$OUT.stdin"\n')
    fake.chmod(0o755)
    out = tmp_path / "call"
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", OUT=str(out))
    subprocess.run(["sh", str(WRAPPER), "skstream-dumb", "1000:1000", str(prefs), "--apply"], env=env, check=True)
    argv = (tmp_path / "call.argv").read_text()
    assert "sekret-token" not in argv and "--apply" in argv and "skstream-dumb" in argv
    assert json.loads((tmp_path / "call.stdin").read_text()) == {"plex_token": "sekret-token", "machine_id": "mid1"}


def test_wrapper_refuses_prefs_without_a_token(tmp_path):
    prefs = tmp_path / "Preferences.xml"
    prefs.write_text('<Preferences FriendlyName="x"/>')
    r = subprocess.run(["sh", str(WRAPPER), "c", "1:1", str(prefs)], capture_output=True, text=True)
    assert r.returncode == 1 and "no PlexOnlineToken" in r.stderr


def test_scripts_are_executable():
    for p in (SCRIPT, WRAPPER):
        assert p.stat().st_mode & stat.S_IXUSR, p


def test_units_run_the_wrapper_with_the_knobs():
    svc, timer = (p.read_text() for p in UNITS)
    assert "/usr/local/sbin/skstream-langguard-run skstream-dumb{{ skstream_sfx }}" in svc
    assert "LANGGUARD_APPLY" in svc and "LANGGUARD_MAX_RESETS" in svc and "Type=oneshot" in svc
    assert "LANGGUARD_INTERVAL" in timer and "WantedBy=timers.target" in timer


def _tasks(env):
    return [t for play in yaml.safe_load(PLAYBOOKS[env].read_text()) for t in play.get("tasks", [])]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_playbooks_install_and_enable_the_guard(env):
    names = {t.get("name", ""): t for t in _tasks(env)}
    inst = names["Install the skstream audio-language guard on the media node"]
    dests = [i["dest"] for i in inst["loop"]]
    assert "{{ skstream_local_root }}/dumb-data/skstream-langguard" in dests  # /data/skstream-langguard inside
    assert "/usr/local/sbin/skstream-langguard-run" in dests
    timer = names["Enable the skstream-langguard timer (or stop it when LANGGUARD_ENABLED is false)"]
    assert timer["systemd"]["name"] == "skstream-langguard{{ skstream_sfx }}.timer"


def test_readme_documents_the_guard_knobs():
    text = README.read_text()
    for knob in ("LANGGUARD_ENABLED", "LANGGUARD_APPLY", "LANGGUARD_MAX_RESETS", "LANGGUARD_INTERVAL",
                 "skstream-langguard", "CLI_DEBRID_PREFERRED_FILTER_OUT"):
        assert knob in text, knob
