"""skstor's Garage metadata db can live on one node's local disk.

Sqlite on the shared NFS tree made a production Garage restart in a loop:
a rescheduled task failed with "Unable to open metadata db: ... Sqlite: timed
out waiting for connection" (the old task's lock) and slow fsyncs timed the
healthcheck out. skstor.META_NODE / META_PATH move the db to local disk on a
pinned node, and META_SNAPSHOT_INTERVAL (default 6h once pinned) snapshots it
back onto the shared tree. Unset, every rendered file is unchanged.
"""
import pathlib
import tomllib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKSTOR_DIR = ANSIBLE / "optional/skstor"
COMPOSE = SKSTOR_DIR / "src/config/skstor/skstor.yml.j2"
GARAGE_TOML = SKSTOR_DIR / "src/config/skstor/garage.toml.j2"
ENVS = ("dev", "staging", "prod")


def _render(path, env_name, **overrides):
    skstor = {"CLUSTERNAME": "site-a", "DOMAIN": "example.com", "rpc_secret": "a" * 64}
    skstor.update(overrides)
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return tpl_env.from_string(path.read_text()).render(app="skstor", env=env_name, skstor=skstor)


def compose(env_name="prod", **overrides):
    return yaml.safe_load(_render(COMPOSE, env_name, **overrides))["services"]["garage"]


def garage_toml(env_name="prod", **overrides):
    return tomllib.loads(_render(GARAGE_TOML, env_name, **overrides))


# --- defaults: nothing changes for an instance that sets none of the vars ---

@pytest.mark.parametrize("env_name", ENVS)
def test_default_keeps_meta_on_the_shared_tree_unpinned_without_snapshots(env_name):
    svc = compose(env_name)
    assert svc["volumes"] == [
        f"/var/data/config/skstor-{env_name}/garage.toml:/etc/garage.toml:ro",
        f"/var/data/skstor-{env_name}/meta:/var/lib/garage/meta",
        f"/var/data/skstor-{env_name}/data:/var/lib/garage/data",
    ]
    assert "placement" not in svc["deploy"]
    doc = garage_toml(env_name)
    assert "metadata_snapshots_dir" not in doc
    assert "metadata_auto_snapshot_interval" not in doc


def test_empty_strings_behave_like_unset():
    svc = compose(META_NODE="", META_PATH="")
    assert "/var/data/skstor-prod/meta:/var/lib/garage/meta" in svc["volumes"]
    assert "placement" not in svc["deploy"]
    assert "metadata_auto_snapshot_interval" not in garage_toml(META_NODE="")


# --- pinned node with local metadata ---

def test_meta_node_pins_the_service_and_mounts_the_local_meta_path():
    svc = compose(META_NODE="node-1", META_PATH="/srv/skstor-prod/meta")
    assert svc["deploy"]["placement"]["constraints"] == ["node.hostname == node-1"]
    assert "/srv/skstor-prod/meta:/var/lib/garage/meta" in svc["volumes"]
    assert not any(v.startswith("/var/data/skstor-prod/meta:") for v in svc["volumes"])
    # data stays on the shared tree
    assert "/var/data/skstor-prod/data:/var/lib/garage/data" in svc["volumes"]


def test_meta_node_defaults_to_six_hour_snapshots_on_the_shared_tree():
    svc = compose(META_NODE="node-1", META_PATH="/srv/skstor-prod/meta")
    assert "/var/data/skstor-prod/meta-snapshots:/var/lib/garage/meta-snapshots" in svc["volumes"]
    doc = garage_toml(META_NODE="node-1")
    assert doc["metadata_auto_snapshot_interval"] == "6h"
    assert doc["metadata_snapshots_dir"] == "/var/lib/garage/meta-snapshots"
    # the db itself stays at the in-container meta path
    assert doc["metadata_dir"] == "/var/lib/garage/meta"


def test_snapshot_interval_is_configurable():
    assert garage_toml(META_NODE="node-1", META_SNAPSHOT_INTERVAL="1h")["metadata_auto_snapshot_interval"] == "1h"


def test_snapshot_interval_can_be_disabled_explicitly_on_a_pinned_node():
    svc = compose(META_NODE="node-1", META_PATH="/srv/m", META_SNAPSHOT_INTERVAL="")
    assert not any("meta-snapshots" in v for v in svc["volumes"])
    assert "metadata_auto_snapshot_interval" not in garage_toml(META_NODE="node-1", META_SNAPSHOT_INTERVAL="")


def test_snapshots_without_a_pin_mount_the_shared_snapshot_dir():
    svc = compose(META_SNAPSHOT_INTERVAL="12h")
    assert "/var/data/skstor-prod/meta-snapshots:/var/lib/garage/meta-snapshots" in svc["volumes"]
    assert "placement" not in svc["deploy"]
    assert garage_toml(META_SNAPSHOT_INTERVAL="12h")["metadata_auto_snapshot_interval"] == "12h"


def test_compose_and_toml_agree_on_when_snapshots_are_on():
    # garage.toml naming a snapshot dir the compose file does not mount would
    # write snapshots into the container's own layer (gone on restart).
    cases = [{}, {"META_NODE": "n"}, {"META_NODE": "n", "META_SNAPSHOT_INTERVAL": ""},
             {"META_SNAPSHOT_INTERVAL": "3h"}]
    for case in cases:
        mounted = any("meta-snapshots" in v for v in compose(**case)["volumes"])
        configured = "metadata_snapshots_dir" in garage_toml(**case)
        assert mounted == configured, case


# --- playbooks: resolve the same values, fail closed, create dirs on the node ---

def _play_tasks(env_name):
    plays = yaml.safe_load((SKSTOR_DIR / f"deploy_skstor-{env_name}.yml").read_text())
    play = next(p for p in plays if p.get("hosts") == "selected_manager_group")
    return play["pre_tasks"], play["tasks"]


def _named(tasks, prefix):
    return next(t for t in tasks if t.get("name", "").startswith(prefix))


@pytest.mark.parametrize("env_name", ENVS)
def test_playbook_fails_closed_on_a_local_meta_path_without_a_node(env_name):
    pre, _ = _play_tasks(env_name)
    names = [t.get("name", "") for t in pre]
    resolve = names.index(next(n for n in names if n.startswith("Resolve the")))
    guard = names.index("Fail closed on a local metadata path without a pinned node")
    assert names.index("Load vault file") < resolve < guard
    that = pre[guard]["assert"]["that"]
    assert any("skstor_meta_path.startswith('/var/data/')" in c for c in that)
    assert any("skstor_meta_node | length > 0" in c for c in that)


def _eval_guard(env_name, meta_node, meta_path):
    pre, _ = _play_tasks(env_name)
    that = _named(pre, "Fail closed on a local metadata path")["assert"]["that"]
    env = jinja2.Environment()
    env.tests["match"] = lambda value, pattern: __import__("re").match(pattern, value) is not None
    ctx = {"skstor_meta_node": meta_node, "skstor_meta_path": meta_path}
    return all(env.compile_expression(cond)(**ctx) for cond in that)


@pytest.mark.parametrize("meta_node,meta_path,ok", [
    ("", "/var/data/skstor-prod/meta", True),        # default
    ("node-1", "/srv/skstor-prod/meta", True),        # pinned local
    ("", "/srv/skstor-prod/meta", False),             # local, unpinned
    ("node-1; rm -rf /", "/srv/m", False),            # not a host name
])
def test_guard_expression(meta_node, meta_path, ok):
    assert _eval_guard("prod", meta_node, meta_path) is ok


@pytest.mark.parametrize("env_name", ENVS)
def test_meta_dir_is_created_on_the_pinned_node_and_old_path_is_left_alone(env_name):
    _, tasks = _play_tasks(env_name)
    base = _named(tasks, "Create the {{ app }} directories")
    assert not any(i["path"].endswith("/meta") for i in base["loop"])
    meta = _named(tasks, "Create the {{ app }} metadata directory")
    assert meta["file"]["path"] == "{{ skstor_meta_path }}"
    assert "skstor_meta_node" in meta["delegate_to"]
    snaps = _named(tasks, "Create the {{ app }} metadata snapshot directory")
    assert snaps["file"]["path"] == "/var/data/{{ app }}-{{ env }}/meta-snapshots"
    assert "skstor_meta_snapshot_interval" in snaps["when"]


@pytest.mark.parametrize("env_name", ENVS)
def test_playbook_defaults_match_the_templates(env_name):
    pre, _ = _play_tasks(env_name)
    facts = _named(pre, "Resolve the")["set_fact"]
    assert "'/var/data/' ~ app ~ '-' ~ env ~ '/meta'" in facts["skstor_meta_path"]
    assert "'6h' if" in facts["skstor_meta_snapshot_interval"]
    for tpl in (COMPOSE, GARAGE_TOML):
        assert "default('6h' if meta_node else '')" in tpl.read_text()
