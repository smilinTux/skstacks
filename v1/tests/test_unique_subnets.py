"""Every overlay network default must have its own subnet: two services
claiming the same (or an overlapping) subnet collide on one swarm. Agents
publishing in parallel each picked "free" ranges and chose the same ones
(skgallery vs sksso). cloud-public-<env> is shared on purpose and may repeat.

The subnets are collected by v1/scripts/networks_registry.py, which reads
every format the tree uses ('name' + env dicts, bare `- subnet:` lists,
per-env maps, deploy.j2 fallbacks), not only the literal-name dict form the
first version of this test matched. Its output is the eyeball-able registry
at v1/ansible/shared/networks.
"""
import importlib.util
import pathlib

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "networks_registry.py"
_spec = importlib.util.spec_from_file_location("networks_registry", SCRIPT)
registry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(registry)


def test_collector_sees_every_format():
    names = {r["name"] for r in registry.collect()}
    # one example per format, so a parser regression cannot pass silently
    for expected in ("skpages-prod", "skform-prod", "skreg-prod", "skwrit-prod (internal)"):
        assert expected in names, expected


def test_no_two_networks_share_a_subnet():
    bad = registry.clashes(registry.collect())
    assert not bad, bad
