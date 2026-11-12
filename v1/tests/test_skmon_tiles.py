"""skmon portal tiles: the Authentik applications the provisioner owns, plus the retired-tile cleanup."""
import importlib.util
import json

import yaml

from skmon_support import (ANSIBLE, CFG, PLAYS, ROLE, TILES, ansible_environment, guard_facts, play_tasks, router_hosts, services,
                           task_named, tile_items, _eval_value)
import pytest

from test_skmon_authentik import FakeAuthentik, ap, input_for, provisioner_input, run_provisioner

SHARED_TILE = ANSIBLE / "shared/src/authentik_portal_tile.py"
_spec = importlib.util.spec_from_file_location("authentik_portal_tile", SHARED_TILE)
tile = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tile)


def tile_vars(item, env="prod", **skmon):
    """The `portal_tile` dict the include in tasks/tiles.yml hands the shared task for one tile."""
    include = task_named(TILES, "Create or update the skmon portal tiles")
    ctx = {**guard_facts(env=env, **skmon), "item": item}
    jenv = ansible_environment()
    return {k: _eval_value(jenv, v, ctx) for k, v in include["vars"]["portal_tile"].items()}


def test_tiles_match_routers():
    items = tile_items()
    assert {i["key"] for i in items} == {"skmon", "vmui", "alertmanager"}
    assert router_hosts(services()) == {f'{i["host_prefix"]}.c1.example.test' for i in items}
    assert {tile_vars(i)["launch_url"].removeprefix("https://") for i in items} == router_hosts(services())


def test_tile_slugs_are_the_provisioners_slugs():
    for env in ("prod", "dev"):
        want = {a["slug"] for a in provisioner_input(env=env, PROFILING={"enabled": True})["apps"]}
        got = {tile_vars(i, env=env)["slug"] for i in tile_items(env=env, PROFILING={"enabled": True})}
        assert got == want, env


def test_every_tile_has_an_icon_file_that_exists():
    for i in tile_items(PROFILING={"enabled": True}):
        # pyroscope.png arrives with Task 22; until then the shared task falls back to the fa:// icon.
        if i["key"] == "pyroscope":
            assert i["icon"].startswith("fa://")
            continue
        assert (CFG / "icons" / i["icon_file"]).exists(), i


def test_retired_icon_is_gone_and_new_one_is_attributed():
    assert not (CFG / "icons/prometheus.png").exists()
    notice = (ANSIBLE / "shared/third_party/dashboard-icons/NOTICE.md").read_text()
    assert "victoriametrics.png" in notice and "prometheus.png" not in notice


def test_pyroscope_tile_only_when_enabled():
    assert "pyroscope" in {i["key"] for i in tile_items(PROFILING={"enabled": True})}
    assert "pyroscope" not in {i["key"] for i in tile_items(PROFILING={"enabled": False})}
    assert "pyroscope" not in {i["key"] for i in tile_items()}


def test_retired_slugs_ride_in_the_provisioner_input_not_a_host_side_uri():
    assert not any("uri" in t for t in yaml.safe_load(TILES.read_text())), "no host-side call to Authentik"
    assert provisioner_input()["retire"] == ["jaeger", "prometheus"]
    assert provisioner_input(env="dev")["retire"] == ["jaeger-dev", "prometheus-dev"]


def test_every_task_touching_the_token_is_no_log():
    for t in yaml.safe_load(TILES.read_text()):
        if "skmon_sksso_token_vault" in json.dumps(t) or "include_vars" in t:
            assert t.get("no_log") is True, t["name"]


def test_every_play_includes_the_tiles_after_the_provisioner_unless_parked():
    jenv = ansible_environment()
    for p in PLAYS:
        tasks = play_tasks(p)
        names = [t.get("name") for t in tasks]
        provision = names.index("Create or update the skmon providers and applications in Authentik")
        inc = next(t for t in tasks if "tasks/tiles.yml" in str(t.get("include_tasks")))
        assert names.index(inc["name"]) > provision, p
        assert not jenv.compile_expression(inc["when"])(skmon_parked=True)
        assert jenv.compile_expression(inc["when"])(skmon_parked=False)


def test_tile_group_is_fixed_and_the_knob_is_retired():
    assert tile_vars(tile_items()[0])["group"] == "skadmin"
    hits = [p for p in ROLE.rglob("*") if p.is_file() and p.suffix != ".pyc" and b"TILE_GROUP" in p.read_bytes()]
    assert hits == []


# --- the shared tile code against the Authentik objects the provisioner made ---------------------

class TileFake(FakeAuthentik):
    """The fake, plus the group filter the real bindings endpoint has and the client's `first`."""
    FILTERS = {**FakeAuthentik.FILTERS, "/policies/bindings/": {"target", "group"}}
    first = tile.Api.first

    def __init__(self):
        super().__init__()
        self.patches = []

    def _rows_for(self, path, q):
        rows = super()._rows_for(path, q)
        if path == "/policies/bindings/" and "group" in q:
            rows = [r for r in rows if r["group"] == q["group"]]
        return rows

    def call(self, method, path, body=None, ok=None):
        if method in ("PATCH", "POST") and "/core/applications/" in path:
            self.patches.append((method, path, body))
        return super().call(method, path, body, ok)


def run_tiles(fake, env="prod", **skmon):
    for item in tile_items(env=env, **skmon):
        cfg = tile_vars(item, env=env, **skmon)
        app = tile.ensure_application(fake, cfg)
        tile.ensure_group_binding(fake, app, cfg["group"])


def test_tile_path_never_clears_the_provider_and_binds_only_skadmin(capsys):
    fake = TileFake()
    run_provisioner(fake, input_for())
    providers = {s: a["provider"] for s, a in fake.apps.items()}
    rows = [dict(r) for r in fake._rows]
    assert all(p is not None for p in providers.values()) and len(providers) == 4
    fake.patches.clear()
    for _ in range(2):  # the second pass proves it is stable
        run_tiles(fake, PROFILING={"enabled": True})
    assert {s: a["provider"] for s, a in fake.apps.items()} == providers
    assert not any("provider" in (b or {}) for _, _, b in fake.patches), fake.patches
    assert fake.bindings == {s: "skadmin" for s in providers}
    assert [dict(r) for r in fake._rows] == rows  # no binding added, changed or removed
    assert fake.deleted == []
    # the tile only adds what the provisioner leaves empty
    assert all(a["meta_icon"] for a in fake.apps.values())


def test_tile_path_is_pinned_to_skadmin_even_for_a_foreign_group_input():
    """The shared task binds the group it is given; tasks/tiles.yml gives it skadmin, a literal."""
    text = yaml.safe_load(TILES.read_text())
    include = next(t for t in text if t.get("name") == "Create or update the skmon portal tiles")
    assert include["vars"]["portal_tile"]["group"] == "skadmin"


# --- retiring the old applications through the provisioner --------------------------------------

class RetireFake(TileFake):
    """The fake, plus DELETE of an application: 204 when present, 404 when absent (or `fail` forces a 500)."""
    fail = False

    def call(self, method, path, body=None, ok=None):
        if method == "DELETE" and path.startswith("/core/applications/"):
            slug = path.removeprefix("/core/applications/").rstrip("/")
            status = 500 if self.fail else (204 if slug in self.apps else 404)
            if status not in (ok or (200, 201, 204)):
                raise ap.ApiError(f"DELETE {path}: HTTP {status}: ", status=status)
            if status == 204:
                self.writes += 1
                self.apps.pop(slug)
                pk = self._app_pk.pop(slug, None)
                self._rows = [r for r in self._rows if r["target"] != pk]
                self.deleted.append(slug)
            return None
        return super().call(method, path, body, ok)


def test_present_retired_slug_is_deleted_and_absent_one_is_a_404_that_succeeds(capsys):
    fake = RetireFake()
    fake.apps["jaeger"] = {"pk": "app-jaeger", "slug": "jaeger", "provider": None}
    fake._app_pk["jaeger"] = "app-jaeger"
    run_provisioner(fake, input_for(retire=["jaeger", "prometheus"]))
    out = capsys.readouterr().out
    assert "retired jaeger: deleted" in out and "retired prometheus: absent" in out
    assert "jaeger" not in fake.apps and fake.deleted == ["jaeger"]
    assert set(fake.apps) == {"skmon", "vmui", "alertmanager", "pyroscope"}


def test_second_run_writes_nothing_more(capsys):
    fake = RetireFake()
    fake.apps["prometheus"] = {"pk": "app-prometheus", "slug": "prometheus", "provider": None}
    fake._app_pk["prometheus"] = "app-prometheus"
    cfg = input_for(retire=["jaeger", "prometheus"])
    run_provisioner(fake, cfg)
    writes = fake.writes
    capsys.readouterr()
    run_provisioner(fake, cfg)  # both absent now: a 404 DELETE is not a write
    assert fake.writes == writes and fake.deleted == ["prometheus"]
    assert capsys.readouterr().out.count("absent") == 2


def test_a_500_on_the_delete_is_an_error():
    fake = RetireFake()
    run_provisioner(fake, input_for())
    fake.fail = True
    with pytest.raises(ap.ApiError, match="HTTP 500"):
        run_provisioner(fake, input_for(retire=["jaeger"]))


def test_main_exits_nonzero_on_a_failed_retire(monkeypatch, capsys):
    import io

    fake = RetireFake()
    fake.fail = True
    monkeypatch.setattr(ap, "Api", lambda *a, **k: fake)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(input_for(retire=["jaeger"]))))
    assert ap.main() == 1
    assert "HTTP 500" in capsys.readouterr().err


def test_a_500_whose_body_mentions_http_404_is_still_an_error():
    class Body404(RetireFake):
        def call(self, method, path, body=None, ok=None):
            if method == "DELETE":
                raise ap.ApiError(f"DELETE {path}: HTTP 500: upstream said HTTP 404 earlier", status=500)
            return super().call(method, path, body, ok)

    fake = Body404()
    with pytest.raises(ap.ApiError, match="HTTP 500"):
        run_provisioner(fake, input_for(retire=["jaeger"]))
