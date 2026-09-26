"""skbook hardcoded `node.role == worker` on bookstack, db and db-backup,
while the live skbook-prod stack has no placement constraint at all (its
tasks run on managers). On skstack01 the whole /var/data tree, including
/var/data/runtime where the db volume's bind device lives, is one NFS export
mounted on every node, so any node can run any of the three services.

`skbook.placement_constraints` (list, default `["node.role == worker"]`, the
previously hardcoded value, so an unset instance renders unchanged) lets an
instance set `[]` to match live without patching the framework."""
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKBOOK = ANSIBLE / "optional/skbook/src/config/skbook/skbook.yml.j2"
SERVICES = ("bookstack", "db", "db-backup")


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return env


def render(**overrides):
    skbook = {"CLUSTERNAME": "skstack01", "DOMAIN": "example.com", **overrides}
    out = _env().from_string(SKBOOK.read_text()).render(app="skbook", env="prod", skbook=skbook)
    return yaml.safe_load(out)["services"]


def constraints_of(svc):
    return ((svc.get("deploy") or {}).get("placement") or {}).get("constraints")


@pytest.mark.parametrize("name", SERVICES)
def test_default_keeps_the_worker_pin(name):
    assert constraints_of(render()[name]) == ["node.role == worker"]


@pytest.mark.parametrize("name", SERVICES)
def test_empty_list_matches_live_no_constraint(name):
    assert constraints_of(render(placement_constraints=[])[name]) == []


@pytest.mark.parametrize("name", SERVICES)
def test_custom_constraints_render_on_every_service(name):
    want = ["node.role == manager", "node.labels.skbook == true"]
    assert constraints_of(render(placement_constraints=want)[name]) == want


def test_db_comment_no_longer_claims_runtime_is_a_local_non_nfs_bind():
    assert "a local bind on the manager, not NFS" not in SKBOOK.read_text()
