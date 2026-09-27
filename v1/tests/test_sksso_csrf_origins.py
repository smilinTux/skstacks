"""sksso's CSRF trusted origins must accept instance-supplied extra origins
(an instance fronting Authentik from another host, e.g. a mesh console,
needs it trusted or Django rejects its POSTs). Default stays sso.* only."""
import pathlib

import jinja2

ENV = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/sksso/src/config/sksso/sksso.env.j2"


def origins(**sksso):
    base = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}
    base.update(sksso)
    out = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True).from_string(
        ENV.read_text()).render(env="prod", sksso=base)
    line = next(l for l in out.splitlines() if l.startswith("AUTHENTIK_CSRF__TRUSTED_ORIGINS="))
    return line.split("=", 1)[1].split(",")


def test_default_trusts_only_the_sso_host():
    assert all("//sso." in o for o in origins())


def test_extra_origins_are_appended():
    got = origins(csrf_extra_origins=["https://mesh.cluster1.example.com"])
    assert "https://mesh.cluster1.example.com" in got
    assert any("//sso." in o for o in got)
