"""sksec on a single-node install: the bouncer can reach skmesh and default@file.

skfenceha has had ``CROWDSEC_ENABLED`` for its ``default@file`` chain, but the
single-node ``skfence`` had no such switch, and the skmesh routers rendered no
middlewares label at all, so a single-node estate running sksec could only
protect services that carry their own middleware knob. Two opt-in settings,
both default off so existing estates render exactly as before:

* ``skfence.CROWDSEC_ENABLED``: puts ``crowdsec-bouncer@file`` first in the
  ``default@file`` chain (same position and semantics as skfenceha).
* ``skmesh.TRAEFIK_MIDDLEWARES``: a list (or comma string) of middlewares for
  every skmesh HTTP router (management API, gRPC, oauth2, signal, relay,
  dashboard).
"""
import pathlib

import jinja2
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKFENCE_MW = ROOT / "core/skfence/src/config/skfence/dynamic-middlewares.yml.j2"
SKMESH_SRC = ROOT / "optional/skmesh/src/config/skmesh"

SKMESH_ROUTERS = [
    "skmesh-mgmt-api",
    "skmesh-mgmt-grpc",
    "skmesh-mgmt-oauth2",
    "skmesh-signal",
    "skmesh-relay",
    "skmesh-dash",
]

SKMESH_BASE = {
    "CLUSTERNAME": "demo",
    "DOMAIN": "example.test",
    "POSTGRES_PASSWORD": "pw",
    "NETBIRD_DATASTORE_ENC_KEY": "enc",
    "NETBIRD_RELAY_AUTH_SECRET": "relaysecret",
    "NETBIRD_RELAY_ENDPOINT": "rels://skmesh.demo.example.test:443",
    "TURN_SECRET": "turnsecret",
    "TURN_USER": "u",
    "TURN_PASSWORD": "p",
}


def _ansible_bool(value):
    """Ansible's ``bool`` filter (the template runs under Ansible)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("yes", "on", "1", "true", "y", "t")


def _skfence_env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = _ansible_bool
    return env


def skfence_default_chain(**skfence_vars):
    """Render skfence's dynamic middlewares and return the default chain."""
    out = _skfence_env().from_string(SKFENCE_MW.read_text()).render(skfence=skfence_vars)
    return yaml.safe_load(out)["http"]["middlewares"]["default"]["chain"]["middlewares"]


def skmesh_labels(**extra):
    """Render the skmesh stack and return {router: middlewares label or None}."""
    jenv = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    out = jenv.from_string((SKMESH_SRC / "skmesh.yml.j2").read_text()).render(
        env="prod", skmesh=dict(SKMESH_BASE, **extra), inventory_hostname="n1"
    )
    labels = []
    for svc in yaml.safe_load(out)["services"].values():
        labels += (svc.get("deploy") or {}).get("labels") or []
    found = {}
    for r in SKMESH_ROUTERS:
        key = f"traefik.http.routers.{r}.middlewares="
        hits = [lbl[len(key):] for lbl in labels if lbl.startswith(key)]
        assert len(hits) <= 1, (r, hits)
        found[r] = hits[0] if hits else None
    return found, labels


# skfence.CROWDSEC_ENABLED


def test_skfence_default_chain_unchanged_without_knob():
    assert skfence_default_chain() == [
        "rate-limit@file",
        "default-security-headers@file",
        "error-pages@file",
        "gzip@file",
    ]


def test_skfence_crowdsec_enabled_puts_bouncer_first():
    chain = skfence_default_chain(CROWDSEC_ENABLED=True)
    assert chain[0] == "crowdsec-bouncer@file"
    assert chain[1:] == skfence_default_chain()


def test_skfence_crowdsec_string_true_turns_on():
    assert skfence_default_chain(CROWDSEC_ENABLED="true")[0] == "crowdsec-bouncer@file"


@pytest.mark.parametrize("value", [False, "false", "no", 0])
def test_skfence_crowdsec_falsy_values_stay_off(value):
    assert "crowdsec-bouncer@file" not in skfence_default_chain(CROWDSEC_ENABLED=value)


def test_skfence_dashboard_chain_never_gets_bouncer():
    out = _skfence_env().from_string(SKFENCE_MW.read_text()).render(skfence={"CROWDSEC_ENABLED": True})
    mw = yaml.safe_load(out)["http"]["middlewares"]
    assert "crowdsec-bouncer@file" not in mw["dashboard-chain"]["chain"]["middlewares"]


# skmesh.TRAEFIK_MIDDLEWARES


def test_skmesh_no_middlewares_label_by_default():
    found, labels = skmesh_labels()
    assert set(found.values()) == {None}
    assert not any(".middlewares=" in lbl for lbl in labels if "routers.skmesh-" in lbl)


def test_skmesh_list_applies_to_every_router():
    found, _ = skmesh_labels(TRAEFIK_MIDDLEWARES=["crowdsec-bouncer@file", "gzip@file"])
    assert found == {r: "crowdsec-bouncer@file,gzip@file" for r in SKMESH_ROUTERS}


def test_skmesh_comma_string_is_trimmed():
    found, _ = skmesh_labels(TRAEFIK_MIDDLEWARES=" crowdsec-bouncer@file , ")
    assert found == {r: "crowdsec-bouncer@file" for r in SKMESH_ROUTERS}


@pytest.mark.parametrize("empty", ["", [], None, " , "])
def test_skmesh_empty_values_render_nothing(empty):
    found, _ = skmesh_labels(TRAEFIK_MIDDLEWARES=empty)
    assert set(found.values()) == {None}


def test_skmesh_knob_applies_in_authentik_mode():
    """Authentik mode has no oauth2 router (Authentik is the IdP); all others get it."""
    found, _ = skmesh_labels(
        TRAEFIK_MIDDLEWARES=["crowdsec-bouncer@file"],
        AUTH_MODE="authentik",
        NETBIRD_AUTH_CLIENT_ID="skmesh",
        IDP_MANAGER_PASSWORD="app-password-0123456789abcdef0123456789",
        SSO_HOST_IP="192.0.2.5",
    )
    expected = {r: "crowdsec-bouncer@file" for r in SKMESH_ROUTERS}
    expected["skmesh-mgmt-oauth2"] = None
    assert found == expected
