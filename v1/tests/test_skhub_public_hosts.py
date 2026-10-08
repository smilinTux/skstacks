"""skhub post-deploy URLs use the public hostnames the routers answer on.

The deploy's post-deploy tasks wrote five Nextcloud settings with a host
built as `skhub.<CLUSTERNAME>.<DOMAIN>` / `collabora.<CLUSTERNAME>.<DOMAIN>`,
hardcoded:

  notify_push:setup and notify_push base_endpoint   https://<skhub>/push
  whiteboard collabBackendUrl                       wss://<skhub>/whiteboard
  richdocuments wopi_url                            https://<collabora>
  spreed signaling_servers                          https://<skhub>/standalone-signaling/

The Traefik routers, OVERWRITEHOST and the Collabora code URL use
`SKHUB_HOSTNAME | default(skhub[-dev|-staging].<base domain>)` and
`COLLABORA_HOSTNAME | default(collabora.<base domain>)`, where the base domain
is `<DOMAIN>` with CLOUDFLARED and `<CLUSTERNAME>.<DOMAIN>` without. Any
instance where those differ from the hardcoded names (CLOUDFLARED, a dev or
staging env, or a set SKHUB_HOSTNAME/COLLABORA_HOSTNAME) had every deploy point
the five settings at names the routers do not answer on: Talk HPB "Unknown
error", no push, no whiteboard, no Collabora.

Now the five use the routers' own expressions. For a prod instance without
CLOUDFLARED and without the two keys (the only case where the old names were
right) every line renders byte-identical to skstacks-v2.26.2.
"""
import pathlib
import re

import jinja2
import pytest
import yaml

SKHUB_DIR = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
PLAYBOOKS = sorted(SKHUB_DIR.glob("deploy_skhub-*.yml"))
PROD = SKHUB_DIR / "deploy_skhub-prod.yml"
CONFIG = SKHUB_DIR / "src/config/skhub"
README = SKHUB_DIR / "README.md"
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "turn_secret": "t" * 32,
        "signaling_secret": "s" * 32, "whiteboard_jwt_secret": "w" * 32,
        "enable_talk_hpb": True, "enable_collabora": True}

# The five lines exactly as skstacks-v2.26.2 shipped them (identical in the
# three env playbooks; YAML strips the block indent).
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
SKHUB_LINES = ("notify_push:setup", "notify_push base_endpoint", "whiteboard collabBackendUrl",
               "spreed signaling_servers")
URL_SHAPE = {
    "notify_push:setup": "https://{}/push",
    "notify_push base_endpoint": "https://{}/push",
    "whiteboard collabBackendUrl": "wss://{}/whiteboard",
    "richdocuments wopi_url": "https://{}",
    "spreed signaling_servers": "https://{}/standalone-signaling/",
}
HOST_VARIANTS = [
    {},
    {"CLOUDFLARED": True},
    {"SKHUB_HOSTNAME": "cloud.example.org"},
    {"COLLABORA_HOSTNAME": "office.example.org"},
    {"SKHUB_HOSTNAME": "cloud.example.org", "COLLABORA_HOSTNAME": "office.example.org", "CLOUDFLARED": True},
]


def _env(**kw):
    return jinja2.Environment(undefined=jinja2.ChainableUndefined, **kw)


def _env_name(pb):
    return pb.stem.rsplit("-", 1)[1]


def _play(pb):
    hits = [p for p in yaml.safe_load(pb.read_text())
            if isinstance(p, dict) and (p.get("vars") or {}).get("app") == "skhub"]
    assert len(hits) == 1
    return hits[0]


def _lines(pb, **over):
    """{marker: rendered line} for the five settings, from the play's own tasks."""
    ctx = {"app": "skhub", "env": _env_name(pb), "skhub": dict(BASE, **over)}
    for k, v in (_play(pb).get("vars") or {}).items():
        ctx[k] = _env().from_string(v).render(**ctx) if isinstance(v, str) else v
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


def _render_config(name, env_name, **over):
    tpl = _env(trim_blocks=True, keep_trailing_newline=True).from_string((CONFIG / name).read_text())
    return tpl.render(app="skhub", env=env_name, skhub=dict(BASE, **over))


def _router_host(compose, router):
    m = re.search(r"traefik\.http\.routers\.%s\.rule=Host\(`([^`]+)`\)" % re.escape(router), compose)
    assert m, router
    return m.group(1)


def _env_value(text, key):
    m = re.search(r"^%s=(.*)$" % key, text, re.M)
    assert m, key
    return m.group(1)


# ---- byte-identical where the old names were right -----------------------------

@pytest.mark.parametrize("over", [{}, {"TURN_DOMAIN": "turn.example.com"}, {"enable_collabora": False}], ids=repr)
def test_prod_default_renders_byte_identical_to_v2_26_2(over):
    got = _lines(PROD, **over)
    ctx = {"skhub": dict(BASE, **over)}
    for marker, old in V2_26_2.items():
        assert got[marker] == _env().from_string(old).render(**ctx), marker


# ---- every env and host setting: the URLs match the routers and OVERWRITEHOST --

@pytest.mark.parametrize("over", HOST_VARIANTS, ids=repr)
@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_urls_use_the_hosts_the_routers_answer_on(pb, over):
    env_name = _env_name(pb)
    compose = _render_config("skhub.yml.j2", env_name, **over)
    env_file = _render_config("skhub.env.j2", env_name, **over)
    skhub_host = _router_host(compose, "skhub-secure")
    collabora_host = _router_host(compose, "collabora-secure")
    assert _env_value(env_file, "OVERWRITEHOST") == skhub_host
    assert _env_value(env_file, "NEXTCLOUD_RICHODOCUMENTS_CODE_URL") == "https://" + collabora_host
    assert _router_host(compose, "talk-hpb-secure").split("`")[0] == skhub_host
    got = _lines(pb, **over)
    for marker in SKHUB_LINES:
        assert _url(marker, got[marker]) == URL_SHAPE[marker].format(skhub_host), marker
    assert _url("richdocuments wopi_url", got["richdocuments wopi_url"]) == "https://" + collabora_host


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_env_suffix_and_cloudflared_defaults(pb):
    env_name = _env_name(pb)
    suffix = "" if env_name == "prod" else "-" + env_name
    got = _lines(pb)
    assert _url("spreed signaling_servers", got["spreed signaling_servers"]) == \
        f"https://skhub{suffix}.cluster1.example.com/standalone-signaling/"
    got = _lines(pb, CLOUDFLARED=True)
    assert _url("whiteboard collabBackendUrl", got["whiteboard collabBackendUrl"]) == f"wss://skhub{suffix}.example.com/whiteboard"
    assert _url("richdocuments wopi_url", got["richdocuments wopi_url"]) == "https://collabora.example.com"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_collabora_hostname_changes_only_wopi_url(pb):
    a, b = _lines(pb), _lines(pb, COLLABORA_HOSTNAME="office.example.org")
    assert _url("richdocuments wopi_url", b["richdocuments wopi_url"]) == "https://office.example.org"
    for marker in SKHUB_LINES:
        assert a[marker] == b[marker], marker


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_only_the_host_changes_in_each_line(pb):
    a = _lines(pb)
    b = _lines(pb, SKHUB_HOSTNAME="cloud.example.org", COLLABORA_HOSTNAME="office.example.org")
    assert '"verify":true' in b["spreed signaling_servers"]
    assert '"secret":"' + "s" * 32 + '"' in b["spreed signaling_servers"]
    for marker in V2_26_2:
        assert a[marker].replace(_url(marker, a[marker]), _url(marker, b[marker])) == b[marker], marker


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_no_cluster_hostname_is_hardcoded_in_the_playbook(pb):
    text = pb.read_text()
    for literal in ("skhub.{{ skhub.CLUSTERNAME }}", "collabora.{{ skhub.CLUSTERNAME }}"):
        assert literal not in text, literal


# ---- talk-hpb.env: TURN_DOMAIN ------------------------------------------------

TURN_BLOCK = ("{% if (skhub.TURN_DOMAIN | default('', true) | string | length) > 0 %}\n"
              "# TURN host Janus hands to clients (README TURN_DOMAIN); unset = NC_DOMAIN.\n"
              "TURN_DOMAIN={{ skhub.TURN_DOMAIN }}\n"
              "{% endif %}\n")


@pytest.mark.parametrize("over", [{}, {"TURN_DOMAIN": ""}, {"TURN_DOMAIN": None}], ids=repr)
@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_talk_hpb_env_unset_turn_domain_renders_as_before(env_name, over):
    src = (CONFIG / "talk-hpb.env.j2").read_text()
    assert src.count(TURN_BLOCK) == 1
    old = _env(trim_blocks=True, keep_trailing_newline=True).from_string(src.replace(TURN_BLOCK, ""))
    new = _render_config("talk-hpb.env.j2", env_name, **over)
    assert new == old.render(app="skhub", env=env_name, skhub=dict(BASE, **over))
    assert "TURN_DOMAIN" not in new


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_talk_hpb_env_renders_turn_domain_when_set(env_name):
    text = _render_config("talk-hpb.env.j2", env_name, TURN_DOMAIN="turn.example.com")
    assert re.findall(r"^TURN_DOMAIN=.*$", text, re.M) == ["TURN_DOMAIN=turn.example.com"]
    assert _env_value(text, "NC_DOMAIN") == _env_value(_render_config("talk-hpb.env.j2", env_name), "NC_DOMAIN")


# ---- docs ---------------------------------------------------------------------

def test_readme_documents_the_hosts_and_the_settings():
    text = README.read_text()
    for key in ("`SKHUB_HOSTNAME`", "`COLLABORA_HOSTNAME`", "signaling_servers", "collabBackendUrl",
                "base_endpoint", "wopi_url", "OVERWRITEHOST", "talk-hpb"):
        assert key in text, key
    assert chr(0x2014) not in text and chr(0x2013) not in text
