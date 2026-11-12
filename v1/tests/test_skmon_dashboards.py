"""Task 18: vendored dashboards (datasource rewrite, SOURCES.yml, PVE-gated Proxmox board)."""
import ast
import json
import re

import yaml

from skmon_support import CFG, PLAYS, ROLE, ansible_environment, guard_facts, play_tasks

DASH = CFG / "dashboards"
SOURCES = yaml.safe_load((DASH / "SOURCES.yml").read_text())
ALLOWED_UIDS = {"skmon-vm", "skmon-loki", "skmon-pyroscope", "-- Grafana --", "-- Mixed --", "-- Dashboard --"}
# Licenses compatible with this AGPL-3.0 repository.
COMPATIBLE = {"Apache-2.0", "MIT", "AGPL-3.0"}


def dashboards():
    return sorted(DASH.rglob("*.json"))


def load(p):
    return json.loads(p.read_text())


def uids(node):
    """Every datasource uid in a dashboard: the uid of each object under a `datasource` key (strings count as uids)."""
    found = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "datasource" and v is not None:
                found.add(v["uid"] if isinstance(v, dict) else v)
            found |= uids(v)
    elif isinstance(node, list):
        for v in node:
            found |= uids(v)
    return found


def var(dash, name):
    return next(v for v in dash["templating"]["list"] if v["name"] == name)


def rel(p):
    return str(p.relative_to(DASH))


def test_every_dashboard_parses_and_has_a_source_entry():
    for p in dashboards():
        load(p)
        assert rel(p) in SOURCES, p
    assert set(SOURCES) == {rel(p) for p in dashboards()}


def test_sources_entries_are_complete_and_licensed():
    for path, e in SOURCES.items():
        assert e["source"] and e["revision"] and e["license"], path
        assert e["license"] in COMPATIBLE, path
        assert isinstance(e["rewritten"], list), path
        if e["source"].startswith("http"):
            assert e["source"].startswith("https://"), path


def test_every_datasource_uid_is_provisioned():
    for p in dashboards():
        assert uids(load(p)) <= ALLOWED_UIDS, p


def test_loki_boards_use_the_loki_uid_for_loki_panels():
    # Every datasource of type loki points at the provisioned Loki uid, and vice versa.
    def walk(n):
        if isinstance(n, dict):
            ds = n.get("datasource")
            if isinstance(ds, dict) and ds.get("uid") in ("skmon-vm", "skmon-loki"):
                assert (ds["type"] == "loki") == (ds["uid"] == "skmon-loki"), ds
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    for p in dashboards():
        walk(load(p))


def test_no_inputs_left():
    for p in dashboards():
        d = load(p)
        assert "__inputs" not in d, p
        assert "${DS_" not in p.read_text(), p


def test_datasource_variables_are_constants():
    for p in dashboards():
        for v in load(p).get("templating", {}).get("list", []):
            assert v["type"] != "datasource", (p, v["name"])


def test_node_exporter_full_job_default():
    v = var(load(DASH / "infrastructure/node-exporter-full.json"), "job")
    assert v["current"]["value"] == "alloy-unix"


def test_dashboard_uids_are_stable_and_unique():
    seen = {}
    for p in dashboards():
        uid = load(p).get("uid")
        assert uid, p
        assert uid not in seen, (p, seen.get(uid))
        seen[uid] = p


def test_retired_dashboards_gone_and_apps_kept():
    names = {p.name for p in DASH.rglob("*.json")}
    assert not {"prometheus.json", "docker-swarm.json", "grafana.json", "traefik.json"} & {p.name for p in DASH.glob("*.json")}
    assert not {"prometheus.json", "docker-swarm.json", "grafana.json"} & names
    assert {"crowdsec.json", "forgejo.json", "qdrant.json"} <= {p.name for p in (DASH / "apps").glob("*.json")}
    assert {"victoriametrics.json", "alloy.json", "loki.json"} <= {p.name for p in (DASH / "skmon internals").glob("*.json")}
    assert {"node-exporter-full.json", "traefik.json"} <= {p.name for p in (DASH / "infrastructure").glob("*.json")}


def _task(play, name):
    return next(t for t in play_tasks(play) if t["name"].startswith(name))


def copied_dashboards(shipped=None, **skmon):
    """Relative paths of the dashboards left on the data node after the plays' copy and prune.

    Evaluates the plays' own expressions: the unwanted list (tasks/facts.yml) and the stale
    computation (the play's `set_fact`). The copy puts every shipped file on the node, the prune
    removes the stale ones. `shipped` overrides the file list (default: the real tree).
    """
    shipped = [rel(p) for p in dashboards()] if shipped is None else list(shipped)
    facts = guard_facts(**skmon)
    src = "/ctl/role/src/config/skmon/dashboards/"
    dst = "/srv/x/grafana-provisioning/dashboards/"
    jenv = ansible_environment()
    outs = []
    for play in PLAYS:
        expr = _task(play, "Work out the stale")["set_fact"]["skmon_dashboards_stale"]
        ctx = {**facts, "playbook_dir": "/ctl/role", "app": "skmon", "skmon_data_path": "/srv/x",
               "skmon_dashboards_shipped": {"files": [{"path": src + r} for r in shipped]},
               "skmon_dashboards_present": {"files": [{"path": dst + r} for r in shipped]}}
        stale = ast.literal_eval(jenv.from_string(expr).render(**ctx).strip())
        outs.append(sorted(r for r in shipped if dst + r not in stale))
    assert outs[0] == outs[1] == outs[2]
    return outs[0]


def test_proxmox_only_with_pve():
    shipped = [*(rel(p) for p in dashboards()), "infrastructure/proxmox.json"]
    assert "infrastructure/proxmox.json" not in copied_dashboards(shipped)
    assert "infrastructure/proxmox.json" not in copied_dashboards(shipped, PVE={"enabled": "false"})
    assert "infrastructure/proxmox.json" in copied_dashboards(shipped, PVE={"enabled": True})
    # Everything else is copied either way.
    assert set(copied_dashboards(shipped)) == set(shipped) - {"infrastructure/proxmox.json"}


def test_no_estate_names_or_hosts():
    for p in dashboards():
        txt = p.read_text()
        assert not re.search(r"\b(\d{1,3}\.){3}\d{1,3}\b", txt), p
        assert not re.search(r"smilintux|skworld|douno\.it|nativeassetmanagement", txt, re.I), p
