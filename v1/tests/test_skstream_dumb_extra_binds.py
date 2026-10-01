"""skstream.DUMB_EXTRA_BINDS: extra bind mounts into the host-level DUMB container.

A production instance had to run a patched cli_debrid module (Other Plex Watchlist for
managed Plex Home users, fixed upstream but not released yet). The fix is a read-only bind of
the patched file over the module inside the DUMB image, added by hand to the host unit. The
unit template had no way to express it, so the next skstream deploy would rewrite the unit
without the bind and silently drop the fix. The knob renders `-v source:target[:ro]` lines
right after the `/data` bind; unset renders the unit byte-identical.
"""
import json
import pathlib
import re

import jinja2
import pytest
import yaml

APP = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skstream"
UNIT = APP / "src/config/skstream/skstream-dumb.service.j2"
README = APP / "README.md"
PLAYBOOKS = {e: APP / f"deploy_skstream-{e}.yml" for e in ("dev", "staging", "prod")}
PATCH = {"source": "/var/lib/skstream/patches/plex_watchlist.py",
         "target": "/cli_debrid/content_checkers/plex_watchlist.py"}


def unit(env="prod", **over):
    j = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    j.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    sk = {"CLUSTERNAME": "demo", "DOMAIN": "example.test", "MEDIA_NODE": "node-a", **over}
    return j.from_string(UNIT.read_text()).render(app="skstream", env=env, skstream=sk)


def v_lines(text):
    return [l.strip().rstrip("\\").strip() for l in text.splitlines() if l.strip().startswith("-v ")]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_unset_renders_byte_identical(env):
    assert unit(env) == unit(env, DUMB_EXTRA_BINDS=[])


def test_bind_is_read_only_by_default_and_follows_data():
    vs = v_lines(unit(DUMB_EXTRA_BINDS=[PATCH]))
    want = "-v /var/lib/skstream/patches/plex_watchlist.py:/cli_debrid/content_checkers/plex_watchlist.py:ro"
    assert want in vs
    assert vs.index(want) == vs.index("-v /var/lib/skstream/dumb-data:/data") + 1


def test_read_only_false_drops_the_suffix():
    vs = v_lines(unit(DUMB_EXTRA_BINDS=[dict(PATCH, read_only=False)]))
    assert "-v /var/lib/skstream/patches/plex_watchlist.py:/cli_debrid/content_checkers/plex_watchlist.py" in vs


def test_several_binds_keep_their_order():
    b2 = {"source": "/srv/x", "target": "/x"}
    vs = v_lines(unit(DUMB_EXTRA_BINDS=[PATCH, b2]))
    i = vs.index("-v /var/lib/skstream/dumb-data:/data")
    assert vs[i + 1].startswith("-v " + PATCH["source"]) and vs[i + 2] == "-v /srv/x:/x:ro"


def test_incomplete_entries_render_nothing():
    base = unit()
    assert unit(DUMB_EXTRA_BINDS=[{"source": "/a"}, {"target": "/b"}, "/c:/d"]) == base


def test_only_extra_lines_are_added():
    a, b = unit().splitlines(), unit(DUMB_EXTRA_BINDS=[PATCH]).splitlines()
    assert len(b) == len(a) + 1
    assert [l for l in b if l not in a] == [
        "  -v /var/lib/skstream/patches/plex_watchlist.py:/cli_debrid/content_checkers/plex_watchlist.py:ro \\"]


def _assert_task(env):
    tasks = [t for play in yaml.safe_load(PLAYBOOKS[env].read_text()) for t in play.get("tasks", [])]
    return [t for t in tasks if "assert" in t and "DUMB_EXTRA_BINDS" in json.dumps(t)]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_playbook_validates_the_binds(env):
    found = _assert_task(env)
    assert len(found) == 1, env
    assert found[0].get("tags") and "config" in found[0]["tags"]


def _evaluate(binds):
    """Run the playbook's own assert expressions through Jinja with Ansible's
    `match` test semantics (re.match)."""
    t = _assert_task("prod")[0]["assert"]["that"]
    j = jinja2.Environment(undefined=jinja2.StrictUndefined)
    j.tests["match"] = lambda v, p: re.match(p, str(v)) is not None
    j.tests["mapping"] = lambda v: isinstance(v, dict)
    j.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    ok = True
    for b in binds:
        for expr in t:
            ok = ok and j.from_string("{{ (" + expr + ") }}").render(item=b, skstream={"DUMB_EXTRA_BINDS": binds}) == "True"
    return ok


def test_assert_accepts_absolute_paths():
    assert _evaluate([PATCH, dict(PATCH, read_only=False)])


@pytest.mark.parametrize("bad", [
    {"source": "relative/x.py", "target": "/x.py"},
    {"source": "/a b.py", "target": "/x.py"},
    {"source": "/a.py", "target": "/x:y.py"},
    {"source": "/a.py"},
    "/a.py:/x.py",
])
def test_assert_rejects_bad_entries(bad):
    assert not _evaluate([bad])


def test_readme_documents_the_knob():
    assert "DUMB_EXTRA_BINDS" in README.read_text()
