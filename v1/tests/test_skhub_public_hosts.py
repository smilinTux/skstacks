"""skhub post-deploy URLs follow the public hostnames (SKHUB_HOSTNAME,
COLLABORA_HOSTNAME).

The deploy's post-deploy tasks wrote five Nextcloud settings with a host
built as `skhub.<CLUSTERNAME>.<DOMAIN>` / `collabora.<CLUSTERNAME>.<DOMAIN>`,
hardcoded:

  notify_push:setup and notify_push base_endpoint   https://<skhub>/push
  whiteboard collabBackendUrl                       wss://<skhub>/whiteboard
  richdocuments wopi_url                            https://<collabora>
  spreed signaling_servers                          https://<skhub>/standalone-signaling/

The Traefik routers, OVERWRITEHOST, notify_push's overwritehost and the
Collabora code URL already use `skhub.SKHUB_HOSTNAME` /
`skhub.COLLABORA_HOSTNAME`. On an instance whose public hosts differ from the
pattern (no cluster label in the name), every deploy set those five to names
that did not resolve or hit another route: Talk HPB "Unknown error", no push,
no whiteboard, no Collabora.

Now they use the same keys, defaulting to the old pattern: with the keys unset
(or empty) every line renders byte-identical to skstacks-v2.26.2.
"""
import pathlib
import re

import jinja2
import pytest
import yaml

SKHUB_DIR = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
PLAYBOOKS = sorted(SKHUB_DIR.glob("deploy_skhub-*.yml"))
COMPOSE = SKHUB_DIR / "src/config/skhub/skhub.yml.j2"
README = SKHUB_DIR / "README.md"
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "turn_secret": "t" * 32,
        "signaling_secret": "s" * 32, "whiteboard_jwt_secret": "w" * 32}

# The five lines exactly as skstacks-v2.26.2 shipped them (identical in the
# three env playbooks; YAML strips the block indent). Keyed by a marker that
# identifies the line.
V2_26_2 = {
    "notify_push:setup":
        'docker exec "$CONTAINER_ID" php occ notify_push:setup "https://skhub.{{ skhub.CLUSTERNAME }}.{{ skhub.DOMAIN }}/push" 2>&1 || true',
    "notify_push base_endpoint":
        'docker exec "$CONTAINER_ID" php occ config:app:set notify_push base_endpoint --value="https://skhub.{{ skhub.CLUSTERNAME }}.{{ skhub.DOMAIN }}/push" 2>&1 || true',
    "whiteboard collabBackendUrl":
        'docker exec "$CONTAINER_ID" php occ config:app:set whiteboard collabBackendUrl --value="wss://skhub.{{ skhub.CLUSTERNAME }}.{{ skhub.DOMAIN }}/whiteboard" 2>&1 || true',
    "richdocuments wopi_url":
        'docker exec "$CONTAINER_ID" php occ config:app:set richdocuments wopi_url --value="https://collabora.{{ skhub.CLUSTERNAME }}.{{ skhub.DOMAIN }}" 2>&1 || true',
    "spreed signaling_servers":
        'docker exec "$CONTAINER_ID" php occ config:app:set spreed signaling_servers --value=\'{"servers":[{"server":"https://skhub.{{ skhub.CLUSTERNAME }}.{{ skhub.DOMAIN }}/standalone-signaling/","verify":true}],"secret":"{{ skhub.signaling_secret }}"}\' 2>&1 || true',
}
SKHUB_HOST_LINES = ("notify_push:setup", "notify_push base_endpoint", "whiteboard collabBackendUrl",
                    "spreed signaling_servers")
EXPECTED_URL = {
    "notify_push:setup": "https://{skhub}/push",
    "notify_push base_endpoint": "https://{skhub}/push",
    "whiteboard collabBackendUrl": "wss://{skhub}/whiteboard",
    "richdocuments wopi_url": "https://{collabora}",
    "spreed signaling_servers": "https://{skhub}/standalone-signaling/",
}
UNSET_VARIANTS = [
    {},
    {"SKHUB_HOSTNAME": "", "COLLABORA_HOSTNAME": ""},
    {"SKHUB_HOSTNAME": None, "COLLABORA_HOSTNAME": None},
    {"CLOUDFLARED": True},
    {"TURN_DOMAIN": "turn.example.com"},
]


def _env():
    return jinja2.Environment(undefined=jinja2.ChainableUndefined)


def _play(pb):
    hits = [p for p in yaml.safe_load(pb.read_text()) if isinstance(p, dict) and (p.get("vars") or {}).get("app") == "skhub"]
    assert len(hits) == 1
    return hits[0]


def _ctx(pb, env_name, **over):
    ctx = {"app": "skhub", "env": env_name, "skhub": dict(BASE, **over)}
    for k, v in (_play(pb).get("vars") or {}).items():
        ctx[k] = _env().from_string(v).render(**ctx) if isinstance(v, str) else v
    return ctx


def _lines(pb, **over):
    """{marker: rendered line} for the five settings, from the play's own tasks."""
    env_name = pb.stem.rsplit("-", 1)[1]
    ctx = _ctx(pb, env_name, **over)
    found = {}
    for task in _play(pb).get("tasks") or []:
        body = task.get("shell")
        if not isinstance(body, str):
            continue
        for line in _env().from_string(body).render(**ctx).splitlines():
            for marker in V2_26_2:
                if marker in line:
                    assert marker not in found, (pb.name, marker)
                    found[marker] = line
    assert set(found) == set(V2_26_2), (pb.name, set(V2_26_2) - set(found))
    return found


def _url(marker, line):
    if marker == "spreed signaling_servers":
        m = re.search(r'"server":"([^"]+)"', line)
    elif marker == "notify_push:setup":
        m = re.search(r'notify_push:setup "([^"]+)"', line)
    else:
        m = re.search(r'--value="([^"]+)"', line)
    assert m, line
    return m.group(1)


@pytest.mark.parametrize("over", UNSET_VARIANTS, ids=repr)
@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_unset_keys_render_byte_identical_to_v2_26_2(pb, over):
    got = _lines(pb, **over)
    ctx = {"skhub": dict(BASE, **over)}
    for marker, old in V2_26_2.items():
        assert got[marker] == _env().from_string(old).render(**ctx), marker


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_skhub_hostname_drives_push_whiteboard_and_signaling(pb):
    got = _lines(pb, SKHUB_HOSTNAME="cloud.example.org")
    for marker in SKHUB_HOST_LINES:
        assert _url(marker, got[marker]) == EXPECTED_URL[marker].format(skhub="cloud.example.org"), marker
    assert _url("richdocuments wopi_url", got["richdocuments wopi_url"]) == "https://collabora.cluster1.example.com"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_collabora_hostname_drives_wopi_url_only(pb):
    a = _lines(pb)
    b = _lines(pb, COLLABORA_HOSTNAME="office.example.org")
    assert _url("richdocuments wopi_url", b["richdocuments wopi_url"]) == "https://office.example.org"
    for marker in SKHUB_HOST_LINES:
        assert a[marker] == b[marker], marker


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_secrets_and_verify_unchanged_when_hosts_set(pb):
    a = _lines(pb)
    b = _lines(pb, SKHUB_HOSTNAME="cloud.example.org", COLLABORA_HOSTNAME="office.example.org")
    assert '"verify":true' in b["spreed signaling_servers"]
    assert '"secret":"' + "s" * 32 + '"' in b["spreed signaling_servers"]
    for marker in V2_26_2:
        old_url, new_url = _url(marker, a[marker]), _url(marker, b[marker])
        assert a[marker].replace(old_url, new_url) == b[marker], marker


def _router_host(text, router):
    m = re.search(r"traefik\.http\.routers\.%s\.rule=Host\(`([^`]+)`\)" % re.escape(router), text)
    assert m, router
    return m.group(1)


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_urls_match_the_traefik_routers_when_hosts_are_set(pb):
    """The point of the fix: Nextcloud is told the names the routers answer on."""
    env_name = pb.stem.rsplit("-", 1)[1]
    over = {"SKHUB_HOSTNAME": "cloud.example.org", "COLLABORA_HOSTNAME": "office.example.org",
            "enable_talk_hpb": True, "enable_collabora": True, "enable_whiteboard": True}
    compose = _env().from_string(COMPOSE.read_text()).render(app="skhub", env=env_name, skhub=dict(BASE, **over))
    skhub_host = _router_host(compose, "skhub-secure")
    collabora_host = _router_host(compose, "collabora-secure")
    got = _lines(pb, **over)
    for marker in SKHUB_HOST_LINES:
        assert _url(marker, got[marker]).split("/")[2] == skhub_host, marker
    assert _url("richdocuments wopi_url", got["richdocuments wopi_url"]).split("/")[2] == collabora_host


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_no_cluster_hostname_is_hardcoded_in_the_playbook(pb):
    text = pb.read_text()
    for literal in ("skhub.{{ skhub.CLUSTERNAME }}", "collabora.{{ skhub.CLUSTERNAME }}"):
        assert literal not in text, literal


def test_readme_documents_both_keys_and_the_settings():
    text = README.read_text()
    for key in ("`SKHUB_HOSTNAME`", "`COLLABORA_HOSTNAME`", "signaling_servers", "collabBackendUrl",
                "base_endpoint", "wopi_url", "OVERWRITEHOST"):
        assert key in text, key
    assert chr(0x2014) not in text and chr(0x2013) not in text
