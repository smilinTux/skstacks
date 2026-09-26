"""Live-parity hook: skvector, skpdf, skorch and sksync each need an
instance-settable placement.constraints. skvector.yml.j2 and skpdf.yml.j2 had
no placement block at all (default = current: no constraint, any node);
skorch had none either; sksync's was hardcoded to []. Live runs
node.role == worker on skvector, and the vault PRs need a knob to reproduce
that without patching the framework."""
import pathlib

import jinja2
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"


def render_services(rel_path, top, **cfg):
    p = ANSIBLE / rel_path
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(p.read_text()).render(env="dev", app=top, **{top: cfg})
    return yaml.safe_load(out)["services"]


def constraints_of(svc):
    return ((svc.get("deploy") or {}).get("placement") or {}).get("constraints")


def test_skvector_defaults_to_no_constraint():
    svcs = render_services("optional/skvector/src/config/skvector/skvector.yml.j2", "skvector")
    assert constraints_of(svcs["qdrant"]) == []


def test_skvector_can_pin_worker_to_match_live():
    svcs = render_services(
        "optional/skvector/src/config/skvector/skvector.yml.j2", "skvector",
        placement_constraints=["node.role == worker"],
    )
    assert constraints_of(svcs["qdrant"]) == ["node.role == worker"]


def test_skpdf_defaults_to_no_constraint_and_is_settable():
    svcs = render_services("optional/skpdf/src/config/skpdf/skpdf.yml.j2", "skpdf")
    assert constraints_of(svcs["stirling-pdf"]) == []
    svcs2 = render_services(
        "optional/skpdf/src/config/skpdf/skpdf.yml.j2", "skpdf",
        placement_constraints=["node.role == worker"],
    )
    assert constraints_of(svcs2["stirling-pdf"]) == ["node.role == worker"]


def test_skorch_every_service_defaults_to_no_constraint_and_is_settable():
    p = ANSIBLE / "optional/skorch/src/config/skorch/skorch.yml.j2"
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    base = env.from_string(p.read_text()).render(
        env="dev", app="skorch", skorch_cfg={}, cluster_name="cluster1", domain="example.com"
    )
    svcs = yaml.safe_load(base)["services"]
    for name, svc in svcs.items():
        assert constraints_of(svc) == [], name

    pinned = env.from_string(p.read_text()).render(
        env="dev", app="skorch", skorch_cfg={"placement_constraints": ["node.role == worker"]},
        cluster_name="cluster1", domain="example.com",
    )
    svcs2 = yaml.safe_load(pinned)["services"]
    for name, svc in svcs2.items():
        assert constraints_of(svc) == ["node.role == worker"], name


def test_sksync_still_defaults_to_no_constraint_and_is_now_settable():
    svcs = render_services("optional/sksync/src/config/sksync/sksync.yml.j2", "sksync")
    assert constraints_of(svcs["syncthing"]) == []
    svcs2 = render_services(
        "optional/sksync/src/config/sksync/sksync.yml.j2", "sksync",
        placement_constraints=["node.role == worker"],
    )
    assert constraints_of(svcs2["syncthing"]) == ["node.role == worker"]
