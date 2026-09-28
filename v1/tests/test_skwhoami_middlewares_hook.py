"""skwhoami is the framework's low-value smoke-test service, which makes it the
natural canary for a new edge middleware (for example putting one router
behind sksec's fail-closed ``crowdsec-bouncer@file`` before widening it).
Its three routers hardcoded ``default-no-crowdsec@file``, so a canary meant
a hand edit of live service labels. ``skwhoami.MIDDLEWARES`` (map of
service name -> Traefik middlewares string) overrides one router's chain;
unset, every router renders exactly as before."""
import pathlib

import jinja2
import pytest
import yaml

TPL = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skwhoami/src/config/skwhoami/skwhoami.yml.j2"
SERVICES = ("whoami01", "whoami02", "whoami03")


def _render(env="prod", **skwhoami):
    j = jinja2.Environment(undefined=jinja2.StrictUndefined)
    out = j.from_string(TPL.read_text()).render(
        env=env, skwhoami=skwhoami, cluster_name="example", domain="example.test"
    )
    return yaml.safe_load(out)["services"]


def _mw(svc):
    labels = svc["deploy"]["labels"]
    found = [l.split("=", 1)[1] for l in labels if ".middlewares=" in l]
    assert len(found) == 1, labels
    return found[0]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_default_is_unchanged(env):
    svcs = _render(env)
    for name in SERVICES:
        assert _mw(svcs[name]) == "default-no-crowdsec@file"


def test_override_changes_only_the_named_router():
    svcs = _render(MIDDLEWARES={"whoami01": "crowdsec-bouncer@file,default@file"})
    assert _mw(svcs["whoami01"]) == "crowdsec-bouncer@file,default@file"
    assert _mw(svcs["whoami02"]) == "default-no-crowdsec@file"
    assert _mw(svcs["whoami03"]) == "default-no-crowdsec@file"


def test_override_keeps_the_router_name():
    svcs = _render(MIDDLEWARES={"whoami02": "default@file"})
    assert any(l.startswith("traefik.http.routers.skwhoami02.middlewares=") for l in svcs["whoami02"]["deploy"]["labels"])


def test_readme_documents_the_key():
    readme = (TPL.parents[3] / "README.md").read_text()
    assert "MIDDLEWARES" in readme
