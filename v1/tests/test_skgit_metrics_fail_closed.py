"""skgit's Forgejo /metrics endpoint must fail closed: with no
skgit.METRICS_TOKEN configured, metrics stay disabled in BOTH the config
Forgejo actually reads (app.ini.j2, rendered by the "Create Forgejo app.ini
configuration file" task) and the env file (skgit.env.j2, kept in sync for
defense in depth even though this framework's entrypoint bypasses the
docker image's own environment-to-ini step). A token turns metrics on with
that token required by Prometheus. Verified live: an empty token let
Forgejo serve /metrics with no auth at all."""
import pathlib

import jinja2

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKGIT_ENV = ANSIBLE / "optional/skgit/src/config/skgit/skgit.env.j2"
APP_INI = ANSIBLE / "optional/skgit/src/config/skgit/app.ini.j2"

BASE_SKGIT = {
    "CLUSTERNAME": "skstack01",
    "DOMAIN": "example.com",
    "POSTGRES_PASSWORD": "pw",
    "SHARED_SECRET": "shared",
    "SECRET_KEY": "secret",
    "INTERNAL_TOKEN": "internal",
    "ADMIN_EMAIL": "admin@example.com",
    "ADMIN_PASSWORD": "adminpw",
}


def _render(template_path, **skgit_overrides):
    skgit = dict(BASE_SKGIT)
    skgit.update(skgit_overrides)
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    tpl_env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("yes", "true", "1")
    return tpl_env.from_string(template_path.read_text()).render(app="skgit", env="prod", skgit=skgit)


def test_env_metrics_disabled_with_no_token():
    out = _render(SKGIT_ENV)
    assert "FORGEJO__metrics__ENABLED=false" in out
    assert "FORGEJO__metrics__TOKEN=" not in out


def test_env_metrics_enabled_with_token():
    out = _render(SKGIT_ENV, METRICS_TOKEN="tok123")
    assert "FORGEJO__metrics__ENABLED=true" in out
    assert "FORGEJO__metrics__TOKEN=tok123" in out


def test_env_explicit_enable_without_token_still_serialises_true_but_deploy_asserts_separately():
    # The template itself just reflects METRICS_ENABLED; the playbook-level
    # assert (test_skgit_metrics_assert_task.py) is what refuses to deploy
    # this combination.
    out = _render(SKGIT_ENV, METRICS_ENABLED=True)
    assert "FORGEJO__metrics__ENABLED=true" in out
    assert "FORGEJO__metrics__TOKEN=" not in out


def test_ini_metrics_disabled_with_no_token():
    out = _render(APP_INI)
    assert "ENABLED = false" in out
    assert "\nTOKEN = " not in out


def test_ini_metrics_enabled_with_token():
    out = _render(APP_INI, METRICS_TOKEN="tok123")
    assert "ENABLED = true" in out
    assert "\nTOKEN = tok123" in out


def test_ini_explicit_disable_wins_even_with_a_token():
    out = _render(APP_INI, METRICS_TOKEN="tok123", METRICS_ENABLED=False)
    assert "ENABLED = false" in out
