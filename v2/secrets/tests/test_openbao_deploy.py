"""Guard tests for the OpenBao server deploy stack (HA Raft, TLS, no-catch-22)."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_OB = Path(__file__).resolve().parents[1] / "openbao"


def test_k8s_ha_raft_three_replicas_tls_on():
    doc = yaml.safe_load((_OB / "k8s-helmchart.yaml").read_text())
    vals = yaml.safe_load(doc["spec"]["valuesContent"])
    ha = vals["server"]["ha"]
    assert ha["enabled"] is True
    assert ha["replicas"] in (3, 5)               # odd Raft quorum
    assert ha["raft"]["enabled"] is True
    assert "tls_disable   = 0" in ha["raft"]["config"]   # TLS on
    assert "service_registration \"kubernetes\"" in ha["raft"]["config"]


def test_k8s_no_plaintext_init_footgun():
    # The deploy must NOT instruct a plaintext init json — that's the catch-22 the
    # PGP bootstrap (../bootstrap.sh) fixes.
    text = (_OB / "k8s-helmchart.yaml").read_text().lower()
    assert "vault-init.json" not in text
    assert "bootstrap.sh" in text                 # points at the no-catch-22 path


def test_swarm_raft_cluster_spread():
    doc = yaml.safe_load((_OB / "swarm-compose.yml").read_text())
    svc = doc["services"]["openbao"]
    assert svc["image"] == "openbao/openbao:2.5.4"
    assert svc["deploy"]["replicas"] == 3
    assert svc["deploy"]["placement"]["max_replicas_per_node"] == 1
    assert "IPC_LOCK" in svc["cap_add"]


def test_server_config_is_raft_with_tls():
    hcl = (_OB / "openbao.hcl").read_text()
    assert 'storage "raft"' in hcl
    assert "tls_disable   = 0" in hcl or "tls_disable = 0" in hcl
    assert "tls_cert_file" in hcl


def test_swarm_tasks_advertise_a_routable_address_not_the_slot():
    # HA correctness: peers must reach each other by a DNS-resolvable per-task
    # address. Advertising the bare node_id (the slot number) breaks Raft quorum.
    doc = yaml.safe_load((_OB / "swarm-compose.yml").read_text())
    svc = doc["services"]["openbao"]
    # each task gets a unique, overlay-resolvable hostname
    assert svc.get("hostname") == "{{.Task.Name}}"
    env = svc["environment"]
    joined = "\n".join(env if isinstance(env, list) else [f"{k}={v}" for k, v in env.items()])
    assert "BAO_RAFT_NODE_ID={{.Task.Slot}}" in joined        # stable raft identity (1,2,3)
    assert "BAO_CLUSTER_ADDR_HOST={{.Task.Name}}" in joined    # routable advertised host


def test_hcl_advertises_the_routable_host_for_api_and_cluster():
    hcl = (_OB / "openbao.hcl").read_text()
    # api/cluster addrs must come from the routable hostname, NOT the node_id slot
    assert 'env "BAO_CLUSTER_ADDR_HOST"' in hcl
    assert 'api_addr     = "https://{{ env "BAO_RAFT_NODE_ID" }}' not in hcl
