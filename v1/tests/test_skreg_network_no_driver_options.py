"""skreg: the overlay network task must not set driver_options.

community.docker.docker_network compares every driver option it is given. An
existing skreg network created without `encrypted: "false"` then reads as
changed, the module tries to recreate it while the registry is attached, and the
deploy fails with "network ... is in use by service" (found on a production
instance during the v2.26.20 rollout). Unencrypted is the overlay default, so the
option added nothing.
"""
import pathlib

import pytest
import yaml

SKREG = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skreg"


def _network_task(env):
    plays = yaml.safe_load((SKREG / f"deploy_skreg-{env}.yml").read_text())
    for play in plays:
        for t in play.get("tasks", []) or []:
            if "community.docker.docker_network" in t:
                return t["community.docker.docker_network"]
    raise AssertionError(f"no docker_network task in deploy_skreg-{env}.yml")


@pytest.mark.parametrize("env", ("dev", "staging", "prod"))
def test_network_task_sets_no_driver_options(env):
    net = _network_task(env)
    assert "driver_options" not in net


@pytest.mark.parametrize("env", ("dev", "staging", "prod"))
def test_network_task_keeps_its_shape(env):
    net = _network_task(env)
    assert net["driver"] == "overlay"
    assert net["attachable"] is True
    assert net["state"] == "present"
    assert len(net["ipam_config"]) == 1 and net["ipam_config"][0]["subnet"].startswith("172.16.")
