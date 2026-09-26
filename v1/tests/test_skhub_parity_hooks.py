"""Live-parity hooks for skhub (live_parity run, framework vs skstack01
prod). Each defaults to the previously rendered value, so an instance that
sets none of them renders unchanged:

- skhub.REDIS_RESOURCES_LIMITS_MEMORY / REDIS_RESOURCES_RESERVATIONS_MEMORY
  (skdash/skmail RESOURCES_* naming, REDIS_ prefixed since skhub has many
  services): redis memory limit/reservation, defaults 256M/64M (live
  1.5G/512M).
- skhub.TALK_HPB_TZ: talk-hpb's TZ, defaulting to skhub.TZ (then UTC), so
  Nextcloud and talk-hpb can differ (live: America/New_York vs UTC)."""
import pathlib

import jinja2
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub/src/config/skhub"
COMPOSE = SKHUB / "skhub.yml.j2"
TALK_ENV = SKHUB / "talk-hpb.env.j2"
NC_ENV = SKHUB / "skhub.env.j2"
BASE_VARS = {"CLUSTERNAME": "skstack01", "DOMAIN": "example.com", "enable_talk_hpb": True}


def _env():
    return jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)


def render(tpl, **overrides):
    return _env().from_string(tpl.read_text()).render(
        app="skhub", env="prod", skhub=dict(BASE_VARS, **overrides), fence_service_name="skfenceha")


def redis_resources(**overrides):
    return yaml.safe_load(render(COMPOSE, **overrides))["services"]["redis"]["deploy"]["resources"]


def env_lines(tpl, **overrides):
    return [l for l in render(tpl, **overrides).splitlines() if l.startswith("TZ=")]


# --- redis resources -------------------------------------------------------

def test_redis_resources_default_unchanged():
    res = redis_resources()
    assert res["limits"] == {"memory": "256M", "cpus": "1.0"}
    assert res["reservations"] == {"memory": "64M", "cpus": ".05"}


def test_redis_resources_overridden_to_live_values():
    res = redis_resources(REDIS_RESOURCES_LIMITS_MEMORY="1.5G", REDIS_RESOURCES_RESERVATIONS_MEMORY="512M")
    assert res["limits"] == {"memory": "1.5G", "cpus": "1.0"}
    assert res["reservations"] == {"memory": "512M", "cpus": ".05"}


def test_redis_override_does_not_touch_other_services():
    services = yaml.safe_load(render(COMPOSE, REDIS_RESOURCES_LIMITS_MEMORY="1.5G"))["services"]
    for name, svc in services.items():
        if name != "redis":
            assert svc.get("deploy", {}).get("resources", {}).get("limits", {}).get("memory") != "1.5G", name


# --- talk-hpb TZ -----------------------------------------------------------

def test_talk_tz_defaults_to_utc_when_nothing_set():
    assert env_lines(TALK_ENV) == ["TZ=UTC"]


def test_talk_tz_follows_skhub_tz_by_default():
    assert env_lines(TALK_ENV, TZ="America/New_York") == ["TZ=America/New_York"]


def test_talk_tz_override_splits_from_nextcloud_tz():
    over = dict(TZ="America/New_York", TALK_HPB_TZ="UTC")
    assert env_lines(TALK_ENV, **over) == ["TZ=UTC"]
    assert env_lines(NC_ENV, **over) == ["TZ=America/New_York"]
