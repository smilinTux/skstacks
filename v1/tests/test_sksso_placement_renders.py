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
