"""skfence error-pages runs on the node that built its image.

The image (skfence-<env>-error-pages:latest) is built by the deploy playbook on
the selected manager and is in no registry. With only node.role == manager,
Swarm could place the task on another manager, where it is refused and
rescheduled, and the image pre-pull step (shared/tasks/prepull_images.yml)
fails on that node. The playbook reads the build node's Swarm node ID and the
stack file pins error-pages to it (found by the v2.26.36 release gate).
"""
import pathlib

import jinja2
import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
SKFENCE = V1 / "ansible/core/skfence"
TEMPLATE = SKFENCE / "src/config/skfence/skfence.yml.j2"


def _render(**extra):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    out = env.from_string(TEMPLATE.read_text()).render(
        app="skfence", env="prod", skfence={"ACME_ENABLED": False}, cluster_name="test", domain="example.com", **extra)
    return yaml.safe_load(out)["services"]


def test_error_pages_is_pinned_to_the_build_node():
    svc = _render(skfence_build_node={"stdout": "abc123nodeid\n", "rc": 0})["error-pages"]
    assert svc["deploy"]["placement"]["constraints"] == ["node.role == manager", "node.id == abc123nodeid"]


def test_without_the_fact_the_template_still_renders_manager_only():
    svc = _render()["error-pages"]
    assert svc["deploy"]["placement"]["constraints"] == ["node.role == manager"]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_playbook_reads_the_node_id_after_the_build_and_before_the_render(env):
    tasks = yaml.safe_load((SKFENCE / f"deploy_skfence-{env}.yml").read_text())[-1]["tasks"]
    names = [t.get("name", "") for t in tasks]
    build = names.index("Build error-pages Docker image")
    read = names.index("Read this node's Swarm node ID (error-pages runs where it was built)")
    render = next(i for i, n in enumerate(names) if n.startswith("Render") and "Compose template" in n)
    assert build < read < render
    task = tasks[read]
    assert task["register"] == "skfence_build_node"
    assert ".Swarm.NodeID" in task["command"]
