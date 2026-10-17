"""skhub Talk federation (skhub.TALK_FEDERATION_ENABLED / TALK_FEDERATION_ALLOWED_GROUPS).

Nextcloud Talk (spreed) supports chat + call federation between two trusted
Nextcloud instances over the standard OCM discovery (/ocm-provider,
/.well-known/ocm; both already enabled by cloud_federation_api /
federatedfilesharing), gated by the app's own occ config:app:set spreed
federation_enabled / federation_allowed_groups appconfig keys. Before this
knob the "Configure Talk signaling and TURN servers in Nextcloud" task never
touched federation_enabled or federation_allowed_groups, so federation stayed
off (Nextcloud's own default for federation_enabled is false).

skhub.TALK_FEDERATION_ENABLED (bool, default false): unset/false renders
exactly as before (no occ federation calls at all). true turns on
federation_enabled for the instance. Needs enable_talk_hpb (federation uses
the same signaling/HPB path as a local call).

skhub.TALK_FEDERATION_ALLOWED_GROUPS (list of group ids, default []): with
TALK_FEDERATION_ENABLED true and this list non-empty, restricts federation to
members of those local groups (Nextcloud Talk's federation_allowed_groups);
empty (the default) leaves federation open to every local user, matching
upstream's own default.
"""
import pathlib
import re

import jinja2
import pytest
import yaml

SKHUB_DIR = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
PLAYBOOKS = sorted(SKHUB_DIR.glob("deploy_skhub-*.yml"))
README = SKHUB_DIR / "README.md"
BASE_VARS = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "turn_secret": "x" * 32,
             "signaling_secret": "s" * 32, "enable_talk_hpb": True}


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    env.filters["to_json"] = __import__("json").dumps
    return env


def _federation_task(pb):
    plays = yaml.safe_load(pb.read_text())
    for play in plays:
        for task in play.get("tasks") or []:
            if task.get("name") == "Configure Talk federation":
                return task
    return None


def _rendered(pb, **overrides):
    task = _federation_task(pb)
    assert task is not None, f"{pb.name}: no Talk federation task found"
    when = task["when"]
    when_true = _env().from_string("{{ (" + when + ") }}").render(
        skhub=dict(BASE_VARS, **overrides),
        nextcloud_container={"stdout": "abc123"},
    )
    shell = task["shell"]
    rendered = _env().from_string(shell).render(skhub=dict(BASE_VARS, **overrides))
    return when_true, rendered


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_task_exists(pb):
    assert _federation_task(pb) is not None, f"{pb.name}: no Talk federation task found"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_default_does_not_run(pb):
    when_true, _ = _rendered(pb)
    assert when_true == "False"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize("falsey", [False, "false", "no", "0", "off"])
def test_falsey_values_do_not_run(pb, falsey):
    when_true, _ = _rendered(pb, TALK_FEDERATION_ENABLED=falsey)
    assert when_true == "False"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_enabled_sets_federation_enabled_yes(pb):
    when_true, rendered = _rendered(pb, TALK_FEDERATION_ENABLED=True)
    assert when_true == "True"
    assert "occ config:app:set spreed federation_enabled --value=yes" in rendered


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_enabled_without_groups_does_not_touch_allowed_groups(pb):
    _, rendered = _rendered(pb, TALK_FEDERATION_ENABLED=True)
    assert "federation_allowed_groups" not in rendered


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_enabled_with_groups_sets_allowed_groups(pb):
    _, rendered = _rendered(pb, TALK_FEDERATION_ENABLED=True, TALK_FEDERATION_ALLOWED_GROUPS=["federation"])
    line = next(l for l in rendered.splitlines() if "federation_allowed_groups" in l)
    m = re.search(r"--value='(\[[^']*\])'", line)
    assert m and m.group(1) == '["federation"]', line


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_needs_enable_talk_hpb(pb):
    when_true, _ = _rendered(pb, TALK_FEDERATION_ENABLED=True, enable_talk_hpb=False)
    assert when_true == "False"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_playbook_still_parses_as_yaml(pb):
    assert yaml.safe_load(pb.read_text())


def test_readme_documents_both_knobs():
    text = README.read_text()
    assert "TALK_FEDERATION_ENABLED" in text
    assert "TALK_FEDERATION_ALLOWED_GROUPS" in text
