"""skhub talk-recording: one replica, and a Firefox policy that turns off ECH.

1. Nextcloud's recording_servers URL is the service VIP
   (http://<app>-<env>_talk-recording:1234). With several replicas behind it,
   the start and the stop of one recording round-robin to different replicas:
   the stop reports "Trying to stop unknown recording" and an orphaned
   recorder stays in the call. One recording server already runs concurrent
   recordings itself (the aio-talk-recording config has no concurrency
   setting), so talk-recording is fixed at 1 replica and
   TALK_RECORDING_MAX_CONCURRENT no longer sets replicas (ignored, with a
   deploy warning when set).

2. When the public skhub host is Cloudflare-proxied with an HTTPS DNS record
   carrying ech=, the recording Firefox intermittently sends the ECH outer SNI
   (cloudflare-ech.com) to the instance's own Traefik, gets the default
   certificate and the recording fails with InsecureCertificate. A locked
   Firefox policy turns ECH and HTTPS-record lookups off; it is rendered
   always (harmless where no ECH record exists) and bind-mounted read-only at
   /usr/lib/firefox/distribution/policies.json.
"""
import json
import pathlib

import jinja2
import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
COMPOSE = SKHUB / "src/config/skhub/skhub.yml.j2"
POLICIES = SKHUB / "src/config/skhub/talk-recording-firefox-policies.json.j2"
README = SKHUB / "README.md"
ENVS = ("dev", "staging", "prod")
PLAYBOOKS = {e: SKHUB / f"deploy_skhub-{e}.yml" for e in ENVS}
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "TALK_RECORDING_ENABLED": True,
        "TALK_RECORDING_NODE": "w2", "TALK_RECORDING_SECRET": "x" * 32}
TARGET = "/usr/lib/firefox/distribution/policies.json"
PREFS = ("network.dns.echconfig.enabled", "network.dns.http3_echconfig.enabled", "network.dns.native_https_query")


def _env():
    return jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)


def recording(env_name="prod", **over):
    text = _env().from_string(COMPOSE.read_text()).render(app="skhub", env=env_name, skhub=dict(BASE, **over),
                                                          fence_service_name="skfenceha")
    return yaml.safe_load(text)["services"]["talk-recording"]


@pytest.mark.parametrize("over", [{}, {"TALK_RECORDING_MAX_CONCURRENT": 3}, {"TALK_RECORDING_MAX_CONCURRENT": 1}], ids=repr)
@pytest.mark.parametrize("env_name", ENVS)
def test_talk_recording_is_one_replica(env_name, over):
    assert recording(env_name, **over)["deploy"]["replicas"] == 1


def test_max_concurrent_no_longer_reaches_the_compose_file():
    assert "TALK_RECORDING_MAX_CONCURRENT" not in COMPOSE.read_text().split("talk-recording:", 1)[1]


@pytest.mark.parametrize("env_name", ENVS)
def test_policies_file_is_bind_mounted_read_only(env_name):
    vols = recording(env_name)["volumes"]
    src = f"/var/data/config/skhub-{env_name}/talk-recording-firefox-policies.json"
    assert vols == [f"{src}:{TARGET}:ro"]


def test_policies_json_locks_ech_off():
    data = json.loads(_env().from_string(POLICIES.read_text()).render(app="skhub", env="prod", skhub=BASE))
    prefs = data["policies"]["Preferences"]
    assert set(prefs) == set(PREFS)
    for name in PREFS:
        assert prefs[name] == {"Value": False, "Status": "locked"}, name


@pytest.mark.parametrize("env_name", ENVS)
def test_policies_template_is_rendered_by_every_playbook(env_name):
    text = PLAYBOOKS[env_name].read_text()
    assert ('{ src: "talk-recording-firefox-policies.json.j2", '
            'dest: "/var/data/config/{{ app }}-{{ env }}/talk-recording-firefox-policies.json", mode: "0644" }') in text


def _tasks(env_name):
    out = []
    for play in yaml.safe_load(PLAYBOOKS[env_name].read_text()):
        if isinstance(play, dict):
            out += (play.get("pre_tasks") or []) + (play.get("tasks") or [])
    return out


@pytest.mark.parametrize("env_name", ENVS)
def test_deploy_warns_when_max_concurrent_is_set(env_name):
    hits = [t for t in _tasks(env_name) if "debug" in t and "TALK_RECORDING_MAX_CONCURRENT" in str(t)]
    assert len(hits) == 1, env_name
    t = hits[0]
    when = t["when"] if isinstance(t["when"], str) else " and ".join(t["when"])
    e = jinja2.Environment()
    expr = e.compile_expression(when)
    assert expr(skhub={"TALK_RECORDING_MAX_CONCURRENT": 3}) and not expr(skhub={})


def test_recording_disabled_renders_no_talk_recording():
    text = _env().from_string(COMPOSE.read_text()).render(app="skhub", env="prod",
                                                          skhub={"CLUSTERNAME": "c", "DOMAIN": "example.com"})
    assert "talk-recording" not in yaml.safe_load(text)["services"]


def test_readme_documents_single_replica_and_ech_policy():
    text = README.read_text()
    for key in ("policies.json", "network.dns.echconfig.enabled", "Trying to stop unknown recording",
                "TALK_RECORDING_MAX_CONCURRENT"):
        assert key in text, key
    assert chr(0x2014) not in text and chr(0x2013) not in text
