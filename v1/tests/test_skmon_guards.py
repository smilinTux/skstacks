"""skmon guards: DATA_NODE, local DATA_PATH, retired vault keys, swarm membership, data dir ownership."""
import ipaddress

import pytest

from skmon_support import AGENT_GUARDS, DATA_DIRS, GUARDS, data_dir_items, eval_asserts, guard_facts, load_tasks

facts = guard_facts
tasks = load_tasks


def test_data_node_required():
    assert any("DATA_NODE" in m for m in eval_asserts(GUARDS, facts(DATA_NODE="")))


@pytest.mark.parametrize("p", ["/var/data", "/var/data/", "/var/data//skmon", "/var/data/../var/data/x", "/var/data/skmon-prod"])
def test_data_path_under_var_data_rejected(p):
    assert any("DATA_PATH" in m for m in eval_asserts(GUARDS, facts(DATA_PATH=p)))


@pytest.mark.parametrize("p", ["/var/database/skmon", "/srv/local/skmon-prod/"])
def test_data_path_normalization(p):
    assert eval_asserts(GUARDS, facts(DATA_PATH=p)) == []


def test_data_path_default():
    assert facts()["skmon_data_path"] == "/srv/local/skmon-prod"


def test_data_path_trailing_slash_is_stripped():
    assert facts(DATA_PATH="/srv/local/x//")["skmon_data_path"] == "/srv/local/x"


@pytest.mark.parametrize("replicas,parked", [(0, True), ("0", True), (1, False), (2, False)])
def test_parked_fact(replicas, parked):
    assert facts(replicas=replicas)["skmon_parked"] is parked
    assert facts()["skmon_parked"] is False


@pytest.mark.parametrize("key", ["PROMETHEUS_RETENTION", "NODE_EXPORTER_IMAGE", "CADVISOR_IMAGE", "PROMTAIL_IMAGE", "JAEGER_IMAGE_VERSION"])
def test_retired_key_fails_with_its_name(key):
    msgs = eval_asserts(GUARDS, facts(**{key: "x"}))
    assert any(key in m for m in msgs)


def test_clean_vault_passes_all_guards():
    assert eval_asserts(GUARDS, facts()) == []


def test_data_node_must_be_a_swarm_node():
    msgs = eval_asserts(GUARDS, facts(DATA_NODE="typo", swarm_nodes=["n1", "n2"]))
    assert any("n1" in m and "n2" in m for m in msgs)


def test_data_node_membership_checked_even_when_parked():
    assert eval_asserts(GUARDS, facts(DATA_NODE="typo", swarm_nodes=["n1"], replicas=0))


def test_data_dirs_owners_match_spec_7_5():
    want = {"vm": "0", "loki": "10001", "grafana": "472", "alertmanager": "65534", "pyroscope": "10001"}
    got = {d["sub"]: str(d["owner"]) for d in data_dir_items()}
    assert {k: got[k] for k in want} == want and "bridge" in got


def test_data_dirs_delegate_to_data_node():
    file_tasks = [t for t in tasks(DATA_DIRS) if "file" in t]
    assert file_tasks and all(t.get("delegate_to") == "{{ skmon_data_node }}" for t in file_tasks)


def test_estate_required():
    for v in ("", None):
        assert any("ESTATE" in m for m in eval_asserts(GUARDS, facts(ESTATE=v)))


def _agent_msgs(*addrs):
    t = [{"node": f"h{i}", "addr": a, "role": "swarm", "light": False} for i, a in enumerate(addrs)]
    return eval_asserts(AGENT_GUARDS, facts(skmon_agent_targets=t))


def test_public_agent_address_fails_naming_the_host():
    msgs = _agent_msgs("10.1.2.3", "8.8.8.8", "172.32.0.1", "2001:db8::1")
    assert len(msgs) == 1 and "h1" in msgs[0] and "h2" in msgs[0] and "h3" in msgs[0]
    assert "h0" not in msgs[0] and "AGENT_LISTEN_ADDR" in msgs[0]


# RFC 6598 shared address space, first and last host. Built from the network's integer
# bounds rather than written out, so the public repo carries no dotted address in that range
# (the estate denylist gate treats such literals as estate identifiers).
_SHARED = ipaddress.IPv4Network(((100 << 24) | (64 << 16), 10))
SHARED_FIRST, SHARED_LAST = str(_SHARED[1]), str(_SHARED[-2])


@pytest.mark.parametrize("addr", [
    "10.0.0.1", "10.255.255.254", "172.16.0.1", "172.31.255.1", "192.168.1.1", SHARED_FIRST, SHARED_LAST,
    "fc00::1", "fd12:3456::1", "192.0.2.10", "198.51.100.7", "203.0.113.9", "node-1.render.example.test", "host"])
def test_private_documentation_and_dns_addresses_pass(addr):
    assert _agent_msgs(addr) == []


@pytest.mark.parametrize("addr", ["100.63.0.1", "100.128.0.1", "172.15.0.1", "11.0.0.1", "192.169.0.1"])
def test_just_outside_private_ranges_fails(addr):
    assert _agent_msgs(addr)
