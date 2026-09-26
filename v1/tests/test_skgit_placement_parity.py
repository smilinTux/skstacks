"""Discovered during the skgit prod vault parity pass (skstacks#49): live runs
node.role == worker on all three skgit services (forgejo, postgres,
db-backup), but the framework had no placement block for any of them.
Adds skgit.placement_constraints (default [] = current behaviour) matching
the pattern already used for skvector/skpdf/skorch/sksync."""
import pathlib

import jinja2
import yaml

SKGIT = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skgit/src/config/skgit/skgit.yml.j2"


def render(**skgit):
    j2 = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = j2.from_string(SKGIT.read_text()).render(env="dev", app="skgit", skgit=skgit, _base_domain="example.com")
    return yaml.safe_load(out)["services"]


def constraints(svc):
    return ((svc.get("deploy") or {}).get("placement") or {}).get("constraints")


def test_all_three_services_default_to_no_constraint():
    svcs = render()
    for name in ("forgejo", "postgres", "db-backup"):
        assert constraints(svcs[name]) == [], name


def test_all_three_services_can_pin_worker_to_match_live():
    svcs = render(placement_constraints=["node.role == worker"])
    for name in ("forgejo", "postgres", "db-backup"):
        assert constraints(svcs[name]) == ["node.role == worker"], name
