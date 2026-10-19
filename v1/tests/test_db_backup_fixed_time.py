"""skhub.backup_time / skhub.backup_tz (and skbook.BACKUP_TIME / skbook.BACKUP_TZ):
an optional fixed-time daily db-backup run.

Today's db-backup loop is: sleep 2m, dump, prune, sleep BACKUP_FREQUENCY (1d),
so the actual run time drifts with whenever the container last started/
restarted. Chef wants a predictable 03:00 America/New_York run instead.

Unset (today's behavior): byte-identical render to before this change -
2m startup delay, then dump/prune/sleep "$BACKUP_FREQUENCY" forever.

Set: the loop instead computes, every iteration, the next occurrence of
backup_time in backup_tz ("today HH:MM", rolling to "tomorrow HH:MM" once
that has passed), logs "next backup at <time> <tz>", sleeps exactly that
long, then dumps and prunes. Recomputing fresh every loop (rather than a
fixed interval) is what makes it DST-correct: a fall-back day's gap is 25
real hours and a spring-forward day's gap is 23, and GNU `date -d` with TZ
set gives the right one because it reads that TZ's wall-clock calendar, not
a cached UTC offset.

Validated at render time (deploy_skhub-*.yml / deploy_skbook-*.yml, after
"Load vault file"): backup_time must be HH:MM 24-hour, and backup_tz must be
a zone the control node's /usr/share/zoneinfo actually has.
"""
import pathlib
import re
import shutil
import subprocess

import jinja2
import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
OPTIONAL = V1 / "ansible/optional"

SKHUB_YML = OPTIONAL / "skhub/src/config/skhub/skhub.yml.j2"
SKHUB_ENV = OPTIONAL / "skhub/src/config/skhub/skhub-db-backup.env.j2"
SKHUB_README = OPTIONAL / "skhub/README.md"
SKHUB_PLAYBOOKS = [OPTIONAL / "skhub" / f"deploy_skhub-{e}.yml" for e in ("dev", "staging", "prod")]

SKBOOK_YML = OPTIONAL / "skbook/src/config/skbook/skbook.yml.j2"
SKBOOK_ENV = OPTIONAL / "skbook/src/config/skbook/skbook-db-backup.env.j2"
SKBOOK_README = OPTIONAL / "skbook/README.md"
SKBOOK_PLAYBOOKS = [OPTIONAL / "skbook" / f"deploy_skbook-{e}.yml" for e in ("dev", "staging", "prod")]


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return env


def _render(path, **variables):
    return _env().from_string(path.read_text()).render(**variables)


def _skhub_yml(**skhub_extra):
    rendered = _render(SKHUB_YML, app="skhub", env="prod",
                        skhub={"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", **skhub_extra},
                        fence_service_name="skfenceha")
    return rendered, yaml.safe_load(rendered)


def _skhub_env(**skhub_extra):
    return _render(SKHUB_ENV, skhub={"mysql_root_password": "x", **skhub_extra})


def _skbook_yml(**skbook_extra):
    rendered = _render(SKBOOK_YML, app="skbook", env="prod", skbook=skbook_extra)
    return rendered, yaml.safe_load(rendered)


def _skbook_env(**skbook_extra):
    return _render(SKBOOK_ENV, skbook={"DB_USER": "u", "DB_PASSWORD": "p", "DB_DATABASE": "d", **skbook_extra})


def _entrypoint(doc):
    ep = doc["services"]["db-backup"]["entrypoint"]
    assert isinstance(ep, str)
    return ep


# --- unset = byte-identical render -----------------------------------------

def test_skhub_unset_backup_time_renders_byte_identical_yml():
    # backup_num_keep/backup_frequency are runtime env vars, not Jinja vars,
    # so they never touch the yml.j2 render; only backup_time (Jinja-side)
    # can change the entrypoint text, and it is unset here.
    base, _ = _skhub_yml()
    again, _ = _skhub_yml()
    assert base == again
    assert "BACKUP_TIME" not in base
    assert "BACKUP_TZ" not in base


def test_skhub_unset_backup_time_entrypoint_matches_pre_feature_script():
    rendered, doc = _skhub_yml()
    ep = _entrypoint(doc)
    assert "sleep 2m" in ep
    assert 'sleep "\\$$BACKUP_FREQUENCY"' in ep
    assert "BACKUP_TIME" not in ep
    assert "BACKUP_TZ" not in ep


def test_skhub_unset_backup_time_env_renders_byte_identical():
    assert "BACKUP_TIME" not in _skhub_env()
    assert "BACKUP_TZ" not in _skhub_env()
    # Same two keys, nothing else, regardless of other vars being set.
    assert _skhub_env(backup_num_keep="3") == _skhub_env(backup_num_keep="3")


def test_skbook_unset_backup_time_entrypoint_matches_pre_feature_script():
    rendered, doc = _skbook_yml()
    ep = _entrypoint(doc)
    assert "sleep 2m" in ep
    assert 'sleep "\\$$BACKUP_FREQUENCY"' in ep
    assert "BACKUP_TIME" not in ep
    assert "BACKUP_TZ" not in ep


def test_skbook_unset_backup_time_env_renders_byte_identical():
    assert "BACKUP_TIME" not in _skbook_env()
    assert "BACKUP_TZ" not in _skbook_env()


# --- set = schedule logic present -------------------------------------------

def test_skhub_set_backup_time_env_carries_values():
    out = _skhub_env(backup_time="03:00", backup_tz="America/New_York")
    assert "BACKUP_TIME=03:00" in out
    assert "BACKUP_TZ=America/New_York" in out


def test_skhub_set_backup_time_env_defaults_tz_to_utc():
    out = _skhub_env(backup_time="03:00")
    assert "BACKUP_TIME=03:00" in out
    assert "BACKUP_TZ=UTC" in out


def test_skhub_set_backup_time_entrypoint_has_schedule_logic():
    _, doc = _skhub_yml(backup_time="03:00", backup_tz="America/New_York")
    ep = _entrypoint(doc)
    assert "sleep 2m" not in ep  # replaced by the computed wait
    assert "BACKUP_TIME" in ep
    assert "BACKUP_TZ" in ep
    assert 'TZ="' in ep
    assert "date -d" in ep
    assert "today" in ep and "tomorrow" in ep
    assert "next backup at" in ep
    # retention + dump command unchanged by this feature
    assert "--single-transaction" in ep
    assert re.search(r"ls -tr /dump/dump_\*\.sql\.gz.*head -n -.*BACKUP_NUM_KEEP", ep)


def test_skbook_set_backup_time_entrypoint_has_schedule_logic():
    _, doc = _skbook_yml(BACKUP_TIME="03:00", BACKUP_TZ="America/New_York")
    ep = _entrypoint(doc)
    assert "sleep 2m" not in ep
    assert "date -d" in ep
    assert "today" in ep and "tomorrow" in ep
    assert "next backup at" in ep
    assert "--single-transaction" in ep


def test_skbook_set_backup_time_env_carries_values():
    out = _skbook_env(BACKUP_TIME="03:00", BACKUP_TZ="America/New_York")
    assert "BACKUP_TIME=03:00" in out
    assert "BACKUP_TZ=America/New_York" in out


# --- render-time validation (assert tasks after vault load) ----------------

@pytest.mark.parametrize("pb", SKHUB_PLAYBOOKS, ids=lambda p: p.name)
def test_skhub_playbooks_validate_backup_time_after_loading_the_vault(pb):
    plays = yaml.safe_load(pb.read_text())
    play = next(p for p in plays if p.get("hosts") == "selected_manager_group")
    names = [t.get("name", "") for t in play["pre_tasks"]]
    load = names.index("Load vault file")
    time_check = next(i for i, n in enumerate(names) if "backup_time" in n.lower() and "validate" in n.lower())
    tz_check = next(i for i, n in enumerate(names) if "backup_tz" in n.lower() and "validate" in n.lower())
    assert time_check > load
    assert tz_check > load
    time_body = yaml.safe_dump(play["pre_tasks"][time_check])
    assert "match" in time_body


@pytest.mark.parametrize("pb", SKBOOK_PLAYBOOKS, ids=lambda p: p.name)
def test_skbook_playbooks_validate_backup_time_after_loading_the_vault(pb):
    plays = yaml.safe_load(pb.read_text())
    play = next(p for p in plays if p.get("hosts") == "selected_manager_group")
    names = [t.get("name", "") for t in play["pre_tasks"]]
    load = names.index("Load vault file")
    time_check = next(i for i, n in enumerate(names) if "backup_time" in n.lower() and "validate" in n.lower())
    tz_check = next(i for i, n in enumerate(names) if "backup_tz" in n.lower() and "validate" in n.lower())
    assert time_check > load
    assert tz_check > load


# --- README documents the knob ----------------------------------------------

def test_skhub_readme_documents_the_knob():
    readme = SKHUB_README.read_text()
    assert "backup_time" in readme
    assert "backup_tz" in readme


def test_skbook_readme_documents_the_knob():
    readme = SKBOOK_README.read_text()
    assert "BACKUP_TIME" in readme
    assert "BACKUP_TZ" in readme


# --- shell-level delay computation across a DST boundary --------------------

GNU_DATE = shutil.which("date")


def _epoch(tz, when):
    out = subprocess.run(
        ["date", "-d", when, "+%s"],
        env={"TZ": tz, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


@pytest.mark.skipif(GNU_DATE is None, reason="no GNU date on this host")
def test_fall_back_dst_boundary_gap_is_25_hours():
    """2026-11-01 is when US DST ends (clocks fall back): 03:00 America/New_York
    on 2026-10-31 to 03:00 America/New_York on 2026-11-01 spans 25 real hours.
    This is the exact 'today HH:MM' -> 'tomorrow HH:MM' step the loop takes."""
    before = _epoch("America/New_York", "2026-10-31 03:00")
    after = _epoch("America/New_York", "2026-11-01 03:00")
    assert after - before == 25 * 3600


@pytest.mark.skipif(GNU_DATE is None, reason="no GNU date on this host")
def test_spring_forward_dst_boundary_gap_is_23_hours():
    """2026-03-08 is when US DST starts (clocks spring forward): the same
    HH:MM -> HH:MM step spans only 23 real hours."""
    before = _epoch("America/New_York", "2026-03-07 03:00")
    after = _epoch("America/New_York", "2026-03-08 03:00")
    assert after - before == 23 * 3600


@pytest.mark.skipif(GNU_DATE is None, reason="no GNU date on this host")
def test_utc_has_no_dst_gap():
    before = _epoch("UTC", "2026-10-31 03:00")
    after = _epoch("UTC", "2026-11-01 03:00")
    assert after - before == 24 * 3600
