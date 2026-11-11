"""optional/skaegis invariants that the generic suites do not check.

- The pinned image defaults in the three deploy plays equal the literals in
  the compose template (list_images.py reads the template).
- Only the dashboard reaches the edge network, with traefik.swarm.* labels
  only; indexer, manager and backup are traefik.enable=false.
- The access model is admin-only on skadmin, never "authentik Admins", in the
  role, its sksso blueprint and its OIDC role mapping.
- The agent version is the same in the Linux playbook, the Windows script and
  the manager image tag.
- The product name is written skaegis, all lowercase.
"""
import pathlib
import re

import pytest
import yaml

from test_compose_templates_schema import make_environment

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
ROLE = ANSIBLE / "optional/skaegis"
COMPOSE = ROLE / "src/config/skaegis/skaegis.yml.j2"
PLAYS = sorted(ROLE.glob("deploy_skaegis-*.yml"))
BLUEPRINT = ANSIBLE / "optional/sksso/src/config/sksso/blueprints/skaegis-oidc.yaml.j2"

PIN_KEYS = {
    "ska_default_indexer_image": "INDEXER_IMAGE",
    "ska_default_manager_image": "MANAGER_IMAGE",
    "ska_default_dashboard_image": "DASHBOARD_IMAGE",
}


def _play_vars(path):
    doc = yaml.safe_load(path.read_text())
    return next(p["vars"] for p in doc if isinstance(p, dict) and p.get("vars", {}).get("app") == "skaegis")


def _render(**over):
    env = make_environment()  # Ansible's jinja2 defaults plus its filters
    ctx = {
        "env": "prod",
        "skaegis": {},
        "ska_router": "skaegis",
        "ska_host": "skaegis.example.test",
        "ska_middlewares": "default@file",
        "ska_sso_middleware": "",
        "ska_tls_options": "intermediate@file",
        "ska_agent_publish": True,
        "ska_agent_publish_mode": "ingress",
        "ska_agent_port": 1514,
        "ska_enrol_port": 1515,
        "ska_indexer_path": "/var/data/skaegis-prod/data/indexer",
        "ska_manager_path": "/var/data/skaegis-prod/data/manager",
        "ska_certs_path": "/var/data/skaegis-prod/data/certs",
        "ska_backup_path": "/var/data/skaegis-prod/backup",
        "ska_auth_mode": "oidc",
        "ska_extra_ca": "",
        "ska_sso_host_ip": "",
    }
    ctx.update(over)
    return yaml.safe_load(env.from_string(COMPOSE.read_text()).render(**ctx))


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_image_pins_match_the_compose_literals(play):
    pvars = _play_vars(play)
    text = COMPOSE.read_text()
    for var, key in PIN_KEYS.items():
        pin = pvars[var]
        assert "@sha256:" in pin, f"{var} is not pinned by digest"
        assert f"skaegis.{key} | default('{pin}', true)" in text, f"compose default for {key} != {play.name} {var}"
    assert "@sha256:" in pvars["ska_default_certs_image"]


def test_only_the_dashboard_is_on_the_edge():
    doc = _render()
    for name, svc in doc["services"].items():
        labels = svc["deploy"]["labels"]
        assert not any(label.startswith("traefik.docker.") for label in labels), name
        nets = svc["networks"]
        on_edge = "cloud-public-prod" in (nets if isinstance(nets, list) else nets.keys())
        if name == "dashboard":
            assert on_edge and "traefik.enable=true" in labels
            assert "traefik.swarm.network=cloud-public-prod" in labels
        else:
            assert not on_edge, f"{name} must not join the edge network"
            assert "traefik.enable=false" in labels, name


def test_stateful_services_need_the_node_label():
    doc = _render()
    for name in ("indexer", "manager", "backup"):
        assert "node.labels.skaegis == true" in doc["services"][name]["deploy"]["placement"]["constraints"]
    pinned = _render(ska_data_node="node-a")
    assert "node.hostname == node-a" in pinned["services"]["indexer"]["deploy"]["placement"]["constraints"]


def test_agent_ports_publish_only_when_asked():
    assert _render()["services"]["manager"]["ports"][1]["target"] == 1515
    assert "ports" not in _render(ska_agent_publish=False)["services"]["manager"]
    for name in ("indexer", "dashboard", "backup"):
        assert "ports" not in _render()["services"][name]


def test_admin_only_on_skadmin_never_authentik_admins():
    texts = [p.read_text() for p in ROLE.rglob("*") if p.is_file() and p.suffix != ".png"]
    texts.append(BLUEPRINT.read_text())
    assert not any("authentik Admins" in t and "never" not in t.lower() for t in texts)
    for play in PLAYS:
        body = play.read_text()
        assert "ska_admin_groups: \"{{ ['skadmin'] }}\"" in body
        assert "AUTHENTIK_TILE_GROUP | default('skadmin', true)" in body
        assert "AUTHENTIK_TILE_NAME | default('skaegis', true)" in body
        assert "ska_access_mode == 'admin-only'" in body
    bp = BLUEPRINT.read_text()
    assert "authentik_policies.policybinding" in bp and "name: skadmin" in bp


def test_agent_version_is_one_value_everywhere():
    manager_tag = re.search(r"wazuh-manager:([0-9.]+)@", COMPOSE.read_text()).group(1)
    linux = re.search(r"AGENT_VERSION \| default\('([^']+)'", (ROLE / "provision_skaegis_agents.yml").read_text()).group(1)
    win = re.search(r"\[string\]\$Version = '([^']+)'", (ROLE / "src/skaegis/windows/install-wazuh-agent.ps1").read_text()).group(1)
    assert linux == win == f"{manager_tag}-1"


def test_product_name_is_lowercase():
    paths = [p for p in ROLE.rglob("*") if p.is_file() and p.suffix != ".png"] + [BLUEPRINT]
    hits = [str(p) for p in paths if "Ae" "gis" in p.read_text()]
    assert not hits, f"write skaegis, all lowercase: {hits}"
