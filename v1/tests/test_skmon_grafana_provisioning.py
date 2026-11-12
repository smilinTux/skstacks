"""Task 17: Grafana provisioning (fixed datasource UIDs, folder layout, home dashboard, DATA_NODE delivery)."""
import pytest
import yaml

from skmon_support import CFG, PLAYS, play_tasks, render

GRAF = CFG / "grafana"


def ds(env="prod", **skmon):
    return yaml.safe_load(render(GRAF / "datasources.yml.j2", env=env, **skmon))


def dash(**skmon):
    return yaml.safe_load(render(GRAF / "dashboards.yml.j2", **skmon))


def env_lines(**skmon):
    return render(CFG / "skmon.env.j2", **skmon).splitlines()


def by_uid(**skmon):
    return {d["uid"]: d for d in ds(**skmon)["datasources"]}


def test_datasource_uids():
    assert set(by_uid()) == {"skmon-vm", "skmon-loki"}
    assert set(by_uid(PROFILING={"enabled": True})) == {"skmon-vm", "skmon-loki", "skmon-pyroscope"}


@pytest.mark.parametrize("off", [False, "false", "False", "0", "no"])
def test_pyroscope_datasource_off_for_falsey_values(off):
    assert "skmon-pyroscope" not in by_uid(PROFILING={"enabled": off})


def test_pyroscope_datasource_on_for_true_string():
    assert "skmon-pyroscope" in by_uid(PROFILING={"enabled": "true"})


def test_datasource_types_and_full_service_urls():
    d = by_uid(env="staging", PROFILING={"enabled": True})
    assert (d["skmon-vm"]["type"], d["skmon-vm"]["url"]) == ("prometheus", "http://skmon-staging_victoriametrics:8428")
    assert (d["skmon-loki"]["type"], d["skmon-loki"]["url"]) == ("loki", "http://skmon-staging_loki:3100")
    assert (d["skmon-pyroscope"]["type"], d["skmon-pyroscope"]["url"]) == (
        "grafana-pyroscope-datasource", "http://skmon-staging_pyroscope:4040")


def test_vm_is_the_only_default():
    d = ds(PROFILING={"enabled": True})["datasources"]
    assert [x["uid"] for x in d if x.get("isDefault")] == ["skmon-vm"]


def test_folders_from_files_and_root_is_the_provider_path():
    opts = dash()["providers"][0]["options"]
    assert opts["foldersFromFilesStructure"] is True
    assert opts["path"] == "/etc/grafana/provisioning/dashboards"
    assert not dash()["providers"][0].get("folder")  # a fixed folder would conflict with the structure


def test_home_dashboard_is_estate_overview_at_provider_root():
    want = "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/etc/grafana/provisioning/dashboards/estate-overview.json"
    assert want in env_lines()


def test_plugins_env_only_when_non_empty():
    assert not any(l.startswith("GF_INSTALL_PLUGINS") for l in env_lines())
    assert not any(l.startswith("GF_INSTALL_PLUGINS") for l in env_lines(GRAFANA_PLUGINS=[]))
    assert "GF_INSTALL_PLUGINS=a-panel,b-panel" in env_lines(GRAFANA_PLUGINS=["a-panel", "b-panel"])


def test_old_templates_are_gone_and_unreferenced():
    assert not (CFG / "grafana-datasources.yml.j2").exists()
    assert not (CFG / "grafana-dashboards.yml.j2").exists()
    for play in PLAYS:
        assert "grafana-datasources.yml.j2" not in play.read_text()
        assert "grafana-dashboards.yml.j2" not in play.read_text()


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_provisioning_written_on_data_node(play):
    tasks = [t for t in play_tasks(play) if "grafana-provisioning" in str(t) and any(k in t for k in ("file", "template", "copy"))]
    assert len(tasks) == 3 and all(t.get("delegate_to") == "{{ skmon_data_node }}" for t in tasks)


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_dashboards_tree_copied_recursively_with_modes(play):
    t = next(t for t in play_tasks(play) if t["name"] == "Copy the dashboards tree to the data node")
    c = t["copy"]
    assert c["src"].endswith("/dashboards/") and c["dest"].endswith("/grafana-provisioning/dashboards/")
    assert (c["mode"], c["directory_mode"]) == ("0644", "0755")


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_folder_directories_created(play):
    dirs = next(t for t in play_tasks(play) if t["name"].startswith("Create the Grafana provisioning directories"))["loop"]
    assert {"dashboards/apps", "dashboards/skmon internals", "dashboards/infrastructure", "datasources"} <= set(dirs)


def _task(play, name):
    return next(t for t in play_tasks(play) if t["name"].startswith(name))


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_prune_task_is_scoped_to_dashboards_json_on_data_node(play):
    names = [t["name"] for t in play_tasks(play)]
    copy_i = names.index("Copy the dashboards tree to the data node")
    assert names.index("Prune stale dashboards from the data node") > copy_i
    present = _task(play, "List the dashboards present")
    assert present["delegate_to"] == "{{ skmon_data_node }}"
    assert present["find"]["paths"] == "{{ skmon_data_path }}/grafana-provisioning/dashboards"
    assert present["find"]["patterns"] == "*.json"
    shipped = _task(play, "List the dashboards the role ships")
    assert shipped["delegate_to"] == "localhost" and shipped["become"] is False
    assert shipped["find"]["patterns"] == "*.json"
    prune = _task(play, "Prune stale dashboards")
    assert prune["delegate_to"] == "{{ skmon_data_node }}"
    assert prune["file"]["state"] == "absent" and prune["file"]["path"] == "{{ item }}"
    assert prune["loop"] == "{{ skmon_dashboards_stale }}"


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_stale_expression_removes_stale_root_file_keeps_wanted(play):
    from skmon_support import ansible_environment
    expr = _task(play, "Work out the stale")["set_fact"]["skmon_dashboards_stale"]
    src = "/ctl/role/src/config/skmon/dashboards"
    dst = "/srv/x/grafana-provisioning/dashboards"
    ctx = {
        "playbook_dir": "/ctl/role", "app": "skmon", "skmon_data_path": "/srv/x",
        "skmon_dashboards_shipped": {"files": [{"path": f"{src}/estate-overview.json"},
                                               {"path": f"{src}/apps/forgejo.json"},
                                               {"path": f"{src}/skmon internals/grafana.json"}]},
        "skmon_dashboards_present": {"files": [{"path": f"{dst}/estate-overview.json"},
                                               {"path": f"{dst}/apps/forgejo.json"},
                                               {"path": f"{dst}/skmon internals/grafana.json"},
                                               {"path": f"{dst}/forgejo.json"},
                                               {"path": f"{dst}/apps/removed.json"}]},
    }
    import ast
    stale = ast.literal_eval(ansible_environment().from_string(expr).render(**ctx).strip())
    assert sorted(stale) == [f"{dst}/apps/removed.json", f"{dst}/forgejo.json"]
    ctx["skmon_dashboards_present"] = {"files": []}
    assert ast.literal_eval(ansible_environment().from_string(expr).render(**ctx).strip()) == []
