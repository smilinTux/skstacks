"""skfenceha's traefik-worker tier carried a hard-coded 0.40 CPU limit. On a
live edge, a single 500 MB WebDAV download through the worker held it at the
limit (about 42% of one core against a 40% cap) and capped throughput near
15 MB/s with memory at 70 MiB of 384 MiB. The limit is now a variable that
defaults to 2 CPUs; the memory limit and the two reservations are variables
that default to their previous values."""
import pathlib

import jinja2
import pytest
import yaml

COMPOSE = (pathlib.Path(__file__).resolve().parents[1]
           / "ansible/core/skfenceha/src/config/skfenceha/skfenceha.yml.j2")


def render(**overrides):
    skfenceha = {"ACME_ENABLED": False}
    skfenceha.update(overrides)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(COMPOSE.read_text()).render(
        app="skfenceha", env="prod", skfenceha=skfenceha, cluster_name="test", domain="example.com")
    return yaml.safe_load(out)["services"]


def worker_resources(**overrides):
    return render(**overrides)["traefik-worker"]["deploy"]["resources"]


def test_defaults_give_two_cpus_and_keep_memory_and_reservations():
    r = worker_resources()
    assert r["limits"] == {"cpus": "2.00", "memory": "384M"}
    assert r["reservations"] == {"cpus": "0.08", "memory": "96M"}


def test_every_knob_overrides_its_value():
    r = worker_resources(TRAEFIK_WORKER_CPUS="4.00", TRAEFIK_WORKER_MEMORY_LIMIT="1024M",
                         TRAEFIK_WORKER_CPUS_RESERVATION="0.50",
                         TRAEFIK_WORKER_MEMORY_RESERVATION="256M")
    assert r["limits"] == {"cpus": "4.00", "memory": "1024M"}
    assert r["reservations"] == {"cpus": "0.50", "memory": "256M"}


@pytest.mark.parametrize("empty", ["", None])
def test_empty_values_fall_back_to_the_defaults(empty):
    r = worker_resources(TRAEFIK_WORKER_CPUS=empty, TRAEFIK_WORKER_MEMORY_LIMIT=empty,
                         TRAEFIK_WORKER_CPUS_RESERVATION=empty,
                         TRAEFIK_WORKER_MEMORY_RESERVATION=empty)
    assert r["limits"] == {"cpus": "2.00", "memory": "384M"}
    assert r["reservations"] == {"cpus": "0.08", "memory": "96M"}


def test_other_services_are_not_affected_by_the_worker_knobs():
    base, tuned = render(), render(TRAEFIK_WORKER_CPUS="4.00", TRAEFIK_WORKER_MEMORY_LIMIT="1024M")
    for name in base:
        if name != "traefik-worker":
            assert base[name] == tuned[name]
