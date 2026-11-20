"""skgit's optional [service] knobs: DISABLE_REGISTRATION, REQUIRE_SIGNIN_VIEW.

An internet-facing Forgejo defaults to open self-registration and anonymous
read of every public repo. These two vault keys let an instance close both
from the rendered app.ini (a live-only edit is reverted by the next deploy,
which re-renders the whole file). Unset, the template renders exactly as
before: no [service] section at all.
"""
import configparser
import pathlib

import jinja2

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
APP_INI = ANSIBLE / "optional/skgit/src/config/skgit/app.ini.j2"

SKGIT = {
    "CLUSTERNAME": "skstack01",
    "DOMAIN": "example.com",
    "POSTGRES_PASSWORD": "pw",
    "SECRET_KEY": "secret",
    "INTERNAL_TOKEN": "internal",
}


def _render(**extra):
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    tpl_env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("yes", "true", "1")
    return tpl_env.from_string(APP_INI.read_text()).render(app="skgit", env="prod", skgit={**SKGIT, **extra})


def _parse(text):
    cfg = configparser.RawConfigParser(strict=True, interpolation=None)
    cfg.optionxform = str
    cfg.read_string("[__root__]\n" + text)
    return cfg


def test_unset_renders_no_service_section():
    assert "[service]" not in _render()


def test_both_knobs_render_lowercase_booleans():
    cfg = _parse(_render(DISABLE_REGISTRATION=True, REQUIRE_SIGNIN_VIEW="yes"))
    assert cfg.get("service", "DISABLE_REGISTRATION") == "true"
    assert cfg.get("service", "REQUIRE_SIGNIN_VIEW") == "true"


def test_one_knob_alone():
    cfg = _parse(_render(DISABLE_REGISTRATION=False))
    assert cfg.get("service", "DISABLE_REGISTRATION") == "false"
    assert not cfg.has_option("service", "REQUIRE_SIGNIN_VIEW")


def test_service_section_does_not_swallow_the_mailer():
    text = _render(DISABLE_REGISTRATION=True, email_enabled=True, email_host="smtp.example.com",
                   email_from="a@example.com", email_username="u", email_password="p")
    cfg = _parse(text)
    assert cfg.get("mailer", "ENABLED") == "true"
    assert not cfg.has_option("service", "ENABLED")


# The deploy waits for Forgejo before bootstrapping the admin user. With
# REQUIRE_SIGNIN_VIEW=true every anonymous /api/v1 request answers 403, so a
# wait on /api/v1/version never succeeds (found by the v2.26.22 lab gate). The
# wait must probe /api/healthz, which stays public.
import re  # noqa: E402

import pytest  # noqa: E402
import yaml  # noqa: E402

PLAYBOOKS = sorted((ANSIBLE / "optional/skgit").glob("deploy_skgit-*.yml"))


def _wait_task(playbook):
    for play in yaml.safe_load(playbook.read_text()):
        for task in play.get("tasks", []) or []:
            if task.get("name") == "Wait for the Forgejo API before creating the admin user":
                return task
    raise AssertionError(f"wait task not found in {playbook.name}")


def test_all_envs_have_a_wait_task():
    assert {p.name for p in PLAYBOOKS} >= {
        "deploy_skgit-dev.yml",
        "deploy_skgit-prod.yml",
        "deploy_skgit-staging.yml",
    }


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_api_wait_probes_public_healthz(playbook):
    cmd = _wait_task(playbook)["shell"]
    assert "localhost:3000/api/healthz" in cmd


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_api_wait_makes_no_anonymous_api_v1_call(playbook):
    cmd = _wait_task(playbook)["shell"]
    calls = [line for line in cmd.splitlines() if "curl" in line and not line.strip().startswith("#")]
    assert calls, "wait task has no curl probe"
    assert not any(re.search(r"/api/v1/", line) for line in calls)
