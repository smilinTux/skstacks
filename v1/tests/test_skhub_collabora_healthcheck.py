"""inc-a2671db2: Collabora 26.04.4.2.1 crash-loops on a production instance
after the v2.25.0 default bump (#137). Root cause (confirmed live on the
affected instance, 2026-10-04): the 26.04 image dropped its shell and `curl`
(minimal, non-root image; WorkingDir /home/nonroot, no /bin/sh) and ships its
OWN healthcheck (`/usr/bin/coolwsd --probe --use-env-vars`, see the image's
Config.Healthcheck). skhub.yml.j2's collabora service hardcodes a
`curl`-based healthcheck test that predates this bump (present since
2026-06-21). On 26.04 that exec always fails ("curl: executable file not
found in $PATH"), Docker marks the task unhealthy after `retries`, and Swarm
restarts it -- every cycle ends with coolwsd printing "Ready to accept
connections" and then an immediate SIGTERM, exit code 0 (not a crash, not
OOM, not apparmor/seccomp -- confirmed via `docker inspect` State and
`docker logs` on the actual cycling containers).

25.04.8.2.1's image (entrypoint /start-collabora-online.sh, a shell script) DOES
have /bin/sh and curl, but its coolwsd binary does NOT support --probe ("Unknown
option specified", exit 78, confirmed live) -- so the native-probe test can't be
made the unconditional default either, or a future pin back to a pre-26.x image
would crash-loop the other way.

Fix: the healthcheck test becomes an override knob (COLLABORA_HEALTHCHECK_TEST),
same pattern as COLLABORA_IMAGE (#137). Its default now matches the framework's
own COLLABORA_IMAGE default (26.04's native probe, no shell/curl needed); anyone
who pins COLLABORA_IMAGE back to a curl-capable pre-26.x build must pin the
matching curl-based test alongside it.
"""
import pathlib

import jinja2
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
COMPOSE = SKHUB / "src/config/skhub/skhub.yml.j2"
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "enable_collabora": True}

COLLABORA_26_04 = "collabora/code:26.04.4.2.1@sha256:4e983196eb9878f339cc506c38c21f1cc3473bca3d6de883c5de08f9c0cc3a6c"
COLLABORA_25_04 = "collabora/code:25.04.8.2.1@sha256:7983fd172c69f4545f93533fee62c06fdcd1aa0806f694db7b9ec6809caa4bd8"
NATIVE_PROBE_TEST = ["CMD", "/usr/bin/coolwsd", "--probe", "--use-env-vars"]
CURL_TEST = ["CMD", "curl", "-f", "http://localhost:9980/hosting/discovery"]


def render(**over):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    return env.from_string(COMPOSE.read_text()).render(app="skhub", env="prod", skhub=dict(BASE, **over),
                                                       fence_service_name="skfenceha")


def collabora(**over):
    return yaml.safe_load(render(**over))["services"]["collabora"]


def test_default_healthcheck_is_the_26_04_native_probe_not_curl():
    """Default COLLABORA_IMAGE is 26.04 (#137); the default healthcheck must match
    what that image can actually run (it has no shell, no curl)."""
    svc = collabora()
    assert svc["image"] == COLLABORA_26_04
    assert svc["healthcheck"]["test"] == NATIVE_PROBE_TEST


def test_default_healthcheck_test_has_no_curl_or_shell_dependency():
    """Permanent regression guard for the actual bug: the default test must never
    again be a curl/CMD-SHELL style exec, because 26.04+ has neither."""
    test = collabora()["healthcheck"]["test"]
    joined = " ".join(test)
    assert "curl" not in joined
    assert "CMD-SHELL" not in test
    assert "/bin/sh" not in joined


def test_healthcheck_test_is_overridable_for_a_pre_26_pin():
    """Pinning COLLABORA_IMAGE back to a curl-capable build (e.g. the incident
    rollback to 25.04.8.2.1) must be able to pin the matching healthcheck alongside it,
    without a framework code change."""
    svc = collabora(COLLABORA_IMAGE=COLLABORA_25_04, COLLABORA_HEALTHCHECK_TEST=CURL_TEST)
    assert svc["image"] == COLLABORA_25_04
    assert svc["healthcheck"]["test"] == CURL_TEST


def test_healthcheck_cadence_is_unchanged():
    """Only the test command changes; interval/timeout/retries/start_period keep
    their existing values (unrelated to this bug)."""
    hc = collabora()["healthcheck"]
    assert hc["interval"] == "30s"
    assert hc["timeout"] == "10s"
    assert hc["retries"] == 5
    assert hc["start_period"] == "90s"
