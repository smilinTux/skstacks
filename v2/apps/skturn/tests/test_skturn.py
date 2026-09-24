"""Guard tests for the skturn (coturn) stack — incl. NO inlined secret."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_SK = Path(__file__).resolve().parents[1]


def _compose():
    return yaml.safe_load((_SK / "docker-compose.yml").read_text())


def test_coturn_image_46():
    assert _compose()["services"]["coturn"]["image"] == "coturn/coturn:4.6"


def test_ports_are_host_mode():
    ports = _compose()["services"]["coturn"]["ports"]
    assert ports and all(p["mode"] == "host" for p in ports)
    published = {(p["published"], p["protocol"]) for p in ports}
    assert (3478, "udp") in published and (5349, "tcp") in published


def test_uses_static_auth_secret_FILE_not_inline_value():
    cmd = _compose()["services"]["coturn"]["command"]
    joined = " ".join(cmd)
    # must reference the secret FILE, never an inline --static-auth-secret=<value>
    assert "--static-auth-secret-file=/run/secrets/coturn_static_auth_secret" in joined
    assert not any(c.startswith("--static-auth-secret=") for c in cmd)


def test_secret_is_external_not_committed():
    sec = _compose()["secrets"]["coturn_static_auth_secret"]
    assert sec.get("external") is True
    # no value/file key that would embed the secret
    assert "file" not in sec and "value" not in sec


def test_pinned_to_edge_node():
    cons = _compose()["services"]["coturn"]["deploy"]["placement"]["constraints"]
    assert any("edge == true" in c for c in cons)


def test_descriptor_marks_shared_secret_do_not_regenerate():
    app = yaml.safe_load((_SK / "app.yaml").read_text())
    sec = app["secrets"][0]
    assert set(sec["shared_with"]) >= {"nextcloud-talk", "netbird"}
    assert "regenerate" in sec["description"].lower()


def test_no_secret_value_or_ncpass_anywhere():
    for f in _SK.rglob("*"):
        if f.is_file() and f.suffix in (".yml", ".yaml", ".md"):
            t = f.read_text()
            # never an inline assignment of the auth secret, never the leaked NC_PASS
            assert "static-auth-secret=" not in t.replace("static-auth-secret-file=", "")
            assert "NBGta" not in t   # fragment of the known-leaked NC app-password
