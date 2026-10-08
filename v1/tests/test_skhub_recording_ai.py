"""skhub Talk recording server + local AI (integration_openai, assistant).

Both are optional and default OFF so an instance that does not set the knobs
renders and deploys exactly as before.

  skhub.TALK_RECORDING_ENABLED       talk-recording service (default false)
  skhub.TALK_RECORDING_IMAGE         digest-pinned aio-talk-recording image
  skhub.TALK_RECORDING_NODE          pins talk-recording (required when enabled,
                                      must differ from skhub.APP_NODE)
  skhub.TALK_RECORDING_SECRET        shared secret, Nextcloud <-> recording (vault,
                                      required when enabled, 32+ chars)
  skhub.TALK_RECORDING_MAX_CONCURRENT  ignored since v2.26.3 (talk-recording is
                                      one replica; was the replica count)

  skhub.AI_ENABLED      installs/enables integration_openai + assistant and
                         points integration_openai at skhub.AI_BASE_URL (default false)
  skhub.AI_BASE_URL     the cluster's skgateway /v1 endpoint (required when enabled)
  skhub.AI_API_KEY      the nextcloud-skhub consumer key (vault, required when enabled)
  skhub.AI_TEXT_MODEL   default_completion_model_id (default sk-default)
  skhub.AI_STT_MODEL    default_stt_model_id (default sk-stt)

AI goes through skgateway and is referenced by model alias only (sk-default,
sk-stt): the occ config never carries an OpenAI cloud model id (gpt-*).
Image generation (t2i) and text-to-speech default ON in integration_openai and
use hardcoded cloud model ids (gpt-image-1-mini, tts-1-hd); both are out of
scope here (design doc "Out of scope"), so AI config turns them off.
"""
import os
import pathlib
import re
import shutil
import subprocess

import jinja2
import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
COMPOSE = SKHUB / "src/config/skhub/skhub.yml.j2"
RECORDING_ENV = SKHUB / "src/config/skhub/talk-recording.env.j2"
README = SKHUB / "README.md"
AI_TASKS = SKHUB / "tasks/ai_config.yml"
RECORDING_TASKS = SKHUB / "tasks/recording_config.yml"
ENVS = ("dev", "staging", "prod")
PLAYBOOKS = {e: SKHUB / f"deploy_skhub-{e}.yml" for e in ENVS}
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    env.tests["match"] = lambda v, pattern: re.match(pattern, str(v)) is not None  # Ansible's match
    return env


def _render(env_name="prod", **over):
    return _env().from_string(COMPOSE.read_text()).render(
        app="skhub", env=env_name, skhub=dict(BASE, **over), fence_service_name="skfenceha")


def _services(env_name="prod", **over):
    return yaml.safe_load(_render(env_name, **over))["services"]


def _render_file(path, env_name="prod", **over):
    return _env().from_string(path.read_text()).render(
        app="skhub", env=env_name, skhub=dict(BASE, **over), fence_service_name="skfenceha")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def render_skhub():
    """render_skhub(vars) -> parsed YAML {"services": {...}} for skhub.yml.j2
    rendered with vars merged over BASE."""
    def _call(overrides, env_name="prod"):
        return {"services": _services(env_name, **overrides)}
    return _call


@pytest.fixture
def run_assert():
    """run_assert(vars) -> the real ansible-playbook exit code for the prod
    playbook's recording/AI required-vars guards, run against vars merged over
    BASE. Extracts the two guard tasks (unchanged) from deploy_skhub-prod.yml
    into a synthetic localhost play, the same approach test_sksso_data_node_unset.py
    and test_skhub_local_webroot.py use for the playbook's own delegated task."""
    def _call(overrides):
        tasks = _guard_tasks("prod")
        return _run_tasks(tasks, dict(BASE, **overrides))
    return _call


@pytest.fixture
def ai_task_commands():
    """ai_task_commands(vars) -> the rendered `occ ...` command lines of every
    task in tasks/ai_config.yml whose `when` is true for vars merged over BASE
    (nextcloud_container/nextcloud_node are stood in as already resolved, the
    same stand-in the real deploy registers before these tasks run)."""
    def _call(overrides):
        return _occ_commands(AI_TASKS, dict(BASE, **overrides))
    return _call


@pytest.fixture
def recording_task_commands():
    def _call(overrides):
        return _occ_commands(RECORDING_TASKS, dict(BASE, **overrides))
    return _call


def _occ_commands(path, skhub_vars):
    ctx = {
        "app": "skhub", "env": "prod", "skhub": skhub_vars,
        "nextcloud_container": {"stdout": "deadbeef0001"},
        "nextcloud_node": {"stdout": "worker1"},
    }
    e = _env()
    tasks = yaml.safe_load(path.read_text())
    out = []
    for t in tasks:
        when = t.get("when")
        if when is not None and not e.compile_expression(when)(**ctx):
            continue
        body = t.get("shell") or t.get("command") or ""
        rendered = e.from_string(body).render(**ctx)
        out += [line.strip() for line in rendered.splitlines() if " occ " in line]
    return out


def _guard_tasks(env_name):
    plays = [p for p in yaml.safe_load(PLAYBOOKS[env_name].read_text()) if isinstance(p, dict)]
    pre = [t for p in plays for t in (p.get("pre_tasks") or [])]
    hits = [t for t in pre if "assert" in t and (
        "TALK_RECORDING_NODE" in str(t.get("vars", {})) or "AI_BASE_URL" in str(t.get("vars", {})))]
    assert len(hits) == 2, f"expected the recording + AI guards, got {len(hits)}"
    return hits


def _run_tasks(tasks, skhub_vars):
    play = [{"hosts": "localhost", "gather_facts": False, "connection": "local",
             "vars": {"app": "skhub", "env": "prod", "skhub": skhub_vars},
             "tasks": tasks}]
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        pb = tmp / "pb.yml"
        pb.write_text(yaml.safe_dump(play, sort_keys=False))
        env_vars = dict(os.environ, HOME=str(tmp), ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="False",
                        ANSIBLE_INVENTORY_UNPARSED_WARNING="False")
        r = subprocess.run(["ansible-playbook", "-i", "localhost,", str(pb)], capture_output=True, text=True,
                            env=env_vars)
        return r.returncode


needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


# ---------------------------------------------------------------------------
# Brief's test cases
# ---------------------------------------------------------------------------

def test_recording_absent_by_default(render_skhub):
    out = render_skhub({})
    assert "talk-recording" not in out["services"]


def test_recording_rendered_when_enabled(render_skhub):
    out = render_skhub({"TALK_RECORDING_ENABLED": True, "TALK_RECORDING_NODE": "w2",
                        "TALK_RECORDING_SECRET": "x" * 32, "APP_NODE": "w3"})
    svc = out["services"]["talk-recording"]
    assert "@sha256:" in svc["image"]
    assert "node.hostname == w2" in svc["deploy"]["placement"]["constraints"]


@needs_ansible
def test_recording_service_is_constrained_to_recording_node_and_refuses_the_app_node(run_assert):
    rc = run_assert({"TALK_RECORDING_ENABLED": True, "TALK_RECORDING_NODE": "w3",
                     "TALK_RECORDING_SECRET": "x" * 32, "APP_NODE": "w3"})
    assert rc != 0


@needs_ansible
def test_recording_enabled_without_secret_fails(run_assert):
    assert run_assert({"TALK_RECORDING_ENABLED": True, "TALK_RECORDING_NODE": "w2"}) != 0


@needs_ansible
def test_ai_enabled_without_ai_base_url_or_ai_api_key_fails_the_assert(run_assert):
    assert run_assert({"AI_ENABLED": True, "AI_API_KEY": "k"}) != 0
    assert run_assert({"AI_ENABLED": True, "AI_BASE_URL": "http://gw.example:18780/v1"}) != 0


def test_ai_occ_commands_use_aliases(ai_task_commands):
    cmds = ai_task_commands({"AI_ENABLED": True, "AI_BASE_URL": "http://gw.example:18780/v1", "AI_API_KEY": "k"})
    joined = "\n".join(cmds)
    assert "app:install integration_openai" in joined or "app:enable integration_openai" in joined
    assert "assistant" in joined
    assert "sk-default" in joined and "sk-stt" in joined
    assert "gpt-" not in joined


def test_ai_tasks_skipped_by_default(ai_task_commands):
    assert ai_task_commands({}) == []


# ---------------------------------------------------------------------------
# Additional coverage beyond the brief's list
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env_name", ENVS)
def test_recording_enabled_matches_the_recording_node_everywhere(env_name, render_skhub):
    out = render_skhub({"TALK_RECORDING_ENABLED": True, "TALK_RECORDING_NODE": "w2",
                        "TALK_RECORDING_SECRET": "x" * 32}, env_name=env_name)
    assert out["services"]["talk-recording"]["deploy"]["placement"]["constraints"] == ["node.hostname == w2"]


def test_recording_service_uses_an_env_file_not_inline_secrets(render_skhub):
    # The compose file itself is deployed world-readable (0644): a secret
    # must never be interpolated directly into it (test_secret_files_not_world_readable.py).
    # talk-recording reads its secrets from talk-recording.env (0640), same as
    # talk-hpb's own env_file.
    out = render_skhub({"TALK_RECORDING_ENABLED": True, "TALK_RECORDING_NODE": "w2",
                        "TALK_RECORDING_SECRET": "a" * 32})
    svc = out["services"]["talk-recording"]
    assert svc["env_file"] == "/var/data/config/skhub-prod/talk-recording.env"
    assert "environment" not in svc
    assert "a" * 32 not in _render(TALK_RECORDING_ENABLED=True, TALK_RECORDING_NODE="w2",
                                   TALK_RECORDING_SECRET="a" * 32)


def test_recording_env_file_carries_the_shared_secrets():
    lines = _render_file(RECORDING_ENV, TALK_RECORDING_SECRET="a" * 32, internal_secret="internal-xyz").splitlines()
    assert "RECORDING_SECRET=" + "a" * 32 in lines
    assert "INTERNAL_SECRET=internal-xyz" in lines
    assert "NC_DOMAIN=skhub.cluster1.example.com" in lines
    assert "HPB_DOMAIN=skhub.cluster1.example.com" in lines


@pytest.mark.parametrize("env_name,suffix", [("dev", "-dev"), ("staging", "-staging"), ("prod", "")])
def test_recording_env_file_domain_matches_the_env(env_name, suffix):
    lines = _render_file(RECORDING_ENV, env_name, TALK_RECORDING_SECRET="a" * 32).splitlines()
    assert f"NC_DOMAIN=skhub{suffix}.cluster1.example.com" in lines


def test_recording_env_template_is_registered_in_every_playbook():
    for env_name in ENVS:
        text = PLAYBOOKS[env_name].read_text()
        assert '{ src: "talk-recording.env.j2", dest: "/var/data/config/{{ app }}-{{ env }}/talk-recording.env", mode: "0640" }' in text, env_name


def test_recording_is_one_replica_whatever_max_concurrent_says(render_skhub):
    # v2.26.3: several replicas behind one VIP break recording stop; see
    # test_skhub_recording_single_ech.py.
    default = render_skhub({"TALK_RECORDING_ENABLED": True, "TALK_RECORDING_NODE": "w2",
                            "TALK_RECORDING_SECRET": "x" * 32})
    assert default["services"]["talk-recording"]["deploy"]["replicas"] == 1
    custom = render_skhub({"TALK_RECORDING_ENABLED": True, "TALK_RECORDING_NODE": "w2",
                           "TALK_RECORDING_SECRET": "x" * 32, "TALK_RECORDING_MAX_CONCURRENT": 3})
    assert custom["services"]["talk-recording"]["deploy"]["replicas"] == 1


def test_recording_image_pinned_by_digest_by_default(render_skhub):
    out = render_skhub({"TALK_RECORDING_ENABLED": True, "TALK_RECORDING_NODE": "w2",
                        "TALK_RECORDING_SECRET": "x" * 32})
    assert out["services"]["talk-recording"]["image"].startswith("ghcr.io/nextcloud-releases/aio-talk-recording:")
    assert "@sha256:" in out["services"]["talk-recording"]["image"]


def test_recording_networks_match_talk_hpb(render_skhub):
    out = render_skhub({"TALK_RECORDING_ENABLED": True, "TALK_RECORDING_NODE": "w2",
                        "TALK_RECORDING_SECRET": "x" * 32, "enable_talk_hpb": True})
    assert set(out["services"]["talk-recording"]["networks"]) == set(out["services"]["talk-hpb"]["networks"])


def test_only_talk_recording_service_appears_when_toggled(render_skhub):
    a, b = render_skhub({}), render_skhub({"TALK_RECORDING_ENABLED": True, "TALK_RECORDING_NODE": "w2",
                                           "TALK_RECORDING_SECRET": "x" * 32})
    assert set(b["services"]) - set(a["services"]) == {"talk-recording"}
    shared = set(a["services"]) & set(b["services"])
    assert all(a["services"][k] == b["services"][k] for k in shared)


@needs_ansible
def test_recording_guard_allows_a_distinct_node(run_assert):
    assert run_assert({"TALK_RECORDING_ENABLED": True, "TALK_RECORDING_NODE": "w2",
                       "TALK_RECORDING_SECRET": "x" * 32, "APP_NODE": "w3"}) == 0


@needs_ansible
def test_recording_guard_allows_disabled_regardless_of_other_keys(run_assert):
    assert run_assert({}) == 0
    assert run_assert({"APP_NODE": "w3"}) == 0


@needs_ansible
def test_ai_guard_allows_enabled_with_both_values(run_assert):
    assert run_assert({"AI_ENABLED": True, "AI_BASE_URL": "http://gw.example:18780/v1", "AI_API_KEY": "k"}) == 0


@needs_ansible
def test_ai_guard_allows_disabled_regardless_of_other_keys(run_assert):
    assert run_assert({}) == 0


def test_recording_task_commands_reference_spreed_recording_servers(recording_task_commands):
    cmds = recording_task_commands({"TALK_RECORDING_ENABLED": True, "TALK_RECORDING_NODE": "w2",
                                    "TALK_RECORDING_SECRET": "a" * 32})
    joined = "\n".join(cmds)
    assert "config:app:set spreed recording_servers" in joined
    assert "a" * 32 in joined


def test_recording_task_commands_skipped_by_default(recording_task_commands):
    assert recording_task_commands({}) == []


def test_readme_documents_the_new_knobs():
    text = README.read_text()
    for key in ("TALK_RECORDING_ENABLED", "TALK_RECORDING_IMAGE", "TALK_RECORDING_NODE", "TALK_RECORDING_SECRET",
                "TALK_RECORDING_MAX_CONCURRENT", "AI_ENABLED", "AI_BASE_URL", "AI_API_KEY", "AI_TEXT_MODEL",
                "AI_STT_MODEL"):
        assert key in text, key
    assert "Talk recording and local AI" in text


def test_changelog_documents_v2_25_0_recording_ai():
    text = (SKHUB.parents[3] / "CHANGELOG.md").read_text()
    assert "TALK_RECORDING_ENABLED" in text
    assert "AI_ENABLED" in text
