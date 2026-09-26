"""skfenceha's worker-role static Traefik config (traefik-worker.yml.j2,
the role that handles all real traffic) shipped no accessLog.fields.headers
block; the framework relied entirely on Traefik's own built-in default.
NAM's old traefik-worker config explicitly listed headers to redact in
access logs. `skfenceha.ACCESS_LOG_HEADERS` ({defaultMode, names}) renders
a static accessLog.fields.headers block; the framework's own default
(unset) redacts Authorization/Cookie/Set-Cookie/X-Api-Key/
Proxy-Authorization -- a safe improvement for every instance, called out in
CHANGELOG as a behaviour change (not "unchanged")."""
import pathlib

import jinja2
import yaml

TPL = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skfenceha/src/config/skfenceha/traefik-worker.yml.j2"

DEFAULT_REDACTED = ("Authorization", "Cookie", "Set-Cookie", "X-Api-Key", "Proxy-Authorization")


def render(**skfenceha_overrides):
    skfenceha = {"ACME_ENABLED": False}
    skfenceha.update(skfenceha_overrides)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(TPL.read_text()).render(env="prod", app="skfenceha", skfenceha=skfenceha)
    return yaml.safe_load(out)


def test_framework_default_redacts_the_safe_default_set():
    headers = render()["accessLog"]["fields"]["headers"]
    assert headers["defaultMode"] == "keep"
    for h in DEFAULT_REDACTED:
        assert headers["names"][h] == "redact", h
    assert len(headers["names"]) == len(DEFAULT_REDACTED)


def test_instance_override_replaces_the_default_set_entirely():
    doc = render(ACCESS_LOG_HEADERS={
        "defaultMode": "keep",
        "names": {
            "Authorization": "redact",
            "Cookie": "redact",
            "Set-Cookie": "redact",
            "X-Auth-Token": "redact",
            "X-Access-Token": "redact",
            "X-Api-Key": "redact",
        },
    })
    names = doc["accessLog"]["fields"]["headers"]["names"]
    assert set(names) == {"Authorization", "Cookie", "Set-Cookie", "X-Auth-Token", "X-Access-Token", "X-Api-Key"}
    assert "Proxy-Authorization" not in names


def test_default_mode_is_overridable_to_drop():
    doc = render(ACCESS_LOG_HEADERS={"defaultMode": "drop", "names": {}})
    assert doc["accessLog"]["fields"]["headers"]["defaultMode"] == "drop"


def test_accesslog_format_is_json_so_entrypoint_can_still_inject_filepath():
    # entrypoint.sh only APPENDS a fallback accessLog: block when one is
    # entirely absent; since this template now always renders one, format
    # must stay json here (that fallback would otherwise have supplied it).
    assert render()["accessLog"]["format"] == "json"
