"""sksso's placement blocks must render a valid mapping in every combination
of the worker constraint and node exclusions. With the worker constraint off,
the old template left `placement:` null (swarm: "must be a mapping"), found on
the skstack06 v2.17.0 run."""
import pathlib

import jinja2
import pytest
import yaml

T = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/sksso/src/config/sksso/sksso.yml.j2"


def render(**sksso):
    base = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}
    base.update(sksso)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return yaml.safe_load(env.from_string(T.read_text()).render(env="dev", app="sksso", sksso=base))


def constraints(doc):
    out = {}
    for name, svc in doc["services"].items():
        placement = (svc.get("deploy") or {}).get("placement", {})
        assert isinstance(placement, dict), f"{name}: placement is {placement!r}"
        out[name] = placement.get("constraints") or []
    return out


@pytest.mark.parametrize("worker", [True, False])
@pytest.mark.parametrize("exclude", [None, ["node-a", "node-b"]])
def test_placement_is_always_a_mapping(worker, exclude):
    kw = {"placement_use_worker_constraint": worker}
    if exclude:
        kw["placement_exclude_nodes"] = exclude
    c = constraints(render(**kw))
    for name, cons in c.items():
        assert all(isinstance(x, str) for x in cons), name
        assert ("node.role == worker" in cons) == worker, name


def test_exclusions_reach_server_and_worker():
    c = constraints(render(placement_use_worker_constraint=False, placement_exclude_nodes=["node-a"]))
    assert "node.hostname != node-a" in c["server"]
    assert "node.hostname != node-a" in c["worker"]


# colocate_with_data_node: pins server + worker to DATA_NODE too, so every
# OIDC token-endpoint request's DB/Redis calls stay on loopback instead of
# crossing the overlay network. Measured gap this closes: see sksso
# README.md "Colocating server + worker with the data node".

def test_colocate_with_data_node_happy_path():
    """Opted in, with DATA_NODE set: server and worker both get pinned,
    alongside postgres and redis (which were already pinned)."""
    c = constraints(render(DATA_NODE="data-1", colocate_with_data_node=True))
    for name in ("postgres", "redis", "server", "worker"):
        assert "node.hostname == data-1" in c[name], name


def test_colocate_with_data_node_requires_data_node():
    """Edge case: opted in but DATA_NODE unset. Nothing to colocate with,
    so no hostname constraint is added anywhere (and the render still
    produces a valid mapping, not a bad Jinja expression)."""
    c = constraints(render(colocate_with_data_node=True))
    for name, cons in c.items():
        assert not any(x.startswith("node.hostname ==") for x in cons), name


def test_colocate_with_data_node_default_is_off():
    """Failure/regression case: DATA_NODE set but colocate_with_data_node
    left at its default (unset). Today's floating behavior for server and
    worker must not change under anyone who already sets DATA_NODE alone."""
    c = constraints(render(DATA_NODE="data-1"))
    assert "node.hostname == data-1" in c["postgres"]
    assert "node.hostname == data-1" in c["redis"]
    assert not any(x.startswith("node.hostname ==") for x in c["server"])
    assert not any(x.startswith("node.hostname ==") for x in c["worker"])
    # explicit False is equivalent to leaving it unset
    c_false = constraints(render(DATA_NODE="data-1", colocate_with_data_node=False))
    assert c_false == c
