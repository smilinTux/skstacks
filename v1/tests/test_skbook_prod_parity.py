"""skbook-prod is a live migrated instance (holobook -> skbook, redeployed
2026-09-25): a from-framework redeploy MUST mount the exact directories the
live stack uses today, or BookStack/MariaDB come up on empty dirs. Verified
against the real prod stack's `docker service inspect` output on its manager:

  bookstack: /var/data/skbook-prod/bookstack/config -> /config
             /var/data/config/skbook-prod/bookstack.env -> /config/.env
  db:        local-driver volume, device=/var/data/runtime/skbook-prod/db -> /var/lib/mysql
  db-backup: /var/data/skbook-prod/database-dump -> /dump

Router/service names also match live (`skbook`, not `skbook-secure`): grepped
the private instance repo and its Traefik instance's config for anything
referencing the router by name, found nothing, so there is no external reason
to keep the `-secure` suffix skhub/skdash use, and matching live removes any
doubt for a service migrating from a legacy playbook.

Sticky-session cookie labels are OFF by default (live sets none); a real
sticky config is opt-in, never silently added by a migration.
"""
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKBOOK = ANSIBLE / "optional/skbook/src/config/skbook/skbook.yml.j2"
DEPLOY_PROD = ANSIBLE / "optional/skbook/deploy_skbook-prod.yml"


def _ansible_bool(v):
    # ansible-playbook provides a real `bool` filter at deploy time; this
    # test's plain jinja2.Environment does not, so it is shimmed here.
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("yes", "true", "1", "on")


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)  # ansible's template default
    env.filters["bool"] = _ansible_bool
    return env


def render(**skbook_overrides):
    skbook = {"CLUSTERNAME": "skstack01", "DOMAIN": "example.com"}
    skbook.update(skbook_overrides)
    out = _env().from_string(SKBOOK.read_text()).render(app="skbook", env="prod", skbook=skbook)
    return yaml.safe_load(out)


def _volumes(doc, service):
    return doc["services"][service]["volumes"]


def _labels(doc, service):
    return doc["services"][service]["deploy"]["labels"]


# --- mount parity (the data-loss risk) -----------------------------------

def test_bookstack_config_mount_matches_prod_layout():
    doc = render()
    volumes = _volumes(doc, "bookstack")
    assert "/var/data/skbook-prod/bookstack/config:/config" in volumes


def test_bookstack_env_file_is_bind_mounted_as_dotenv():
    """BookStack's own Laravel app reads /config/.env as a literal file, not
    just process environment variables -- the live stack bind-mounts the
    rendered bookstack.env there directly."""
    doc = render()
    volumes = _volumes(doc, "bookstack")
    assert "/var/data/config/skbook-prod/bookstack.env:/config/.env" in volumes


def test_db_volume_device_matches_prod_runtime_path():
    doc = render()
    top_volumes = doc.get("volumes", {})
    db_vol = next(iter(top_volumes.values()))
    assert db_vol["driver_opts"]["device"] == "/var/data/runtime/skbook-prod/db"


def test_db_backup_dump_mount_matches_prod_layout():
    doc = render()
    volumes = _volumes(doc, "db-backup")
    assert "/var/data/skbook-prod/database-dump:/dump" in volumes


@pytest.mark.parametrize("env,expected", [("staging", "/var/data/skbook-staging/bookstack/config:/config"),
                                           ("dev", "/var/data/skbook-dev/bookstack/config:/config")])
def test_bookstack_config_mount_layout_is_the_same_shape_in_every_env(env, expected):
    skbook = {"CLUSTERNAME": "skstack01", "DOMAIN": "example.com"}
    out = _env().from_string(SKBOOK.read_text()).render(app="skbook", env=env, skbook=skbook)
    doc = yaml.safe_load(out)
    assert expected in doc["services"]["bookstack"]["volumes"]


# --- router/service naming matches live -----------------------------------

def test_router_and_service_name_match_live_prod():
    doc = render()
    labels = _labels(doc, "bookstack")
    assert "traefik.http.routers.skbook.rule=Host(`skbook.skstack01.example.com`)" in labels
    assert "traefik.http.routers.skbook.service=skbook" in labels
    assert "traefik.http.services.skbook.loadbalancer.server.port=80" in labels
    assert not any("routers.skbook-secure" in l or "services.skbook-secure" in l for l in labels)


def test_loadbalancer_scheme_label_present():
    """Live sets loadbalancer.server.scheme=http explicitly; the framework
    template was missing it."""
    doc = render()
    labels = _labels(doc, "bookstack")
    assert "traefik.http.services.skbook.loadbalancer.server.scheme=http" in labels


def test_tls_options_and_middlewares_hooks_still_work_with_the_new_router_name():
    doc = render(tls_options="intermediate@file", router_middlewares=["default-no-crowdsec@file"])
    labels = _labels(doc, "bookstack")
    assert "traefik.http.routers.skbook.tls.options=intermediate@file" in labels
    assert "traefik.http.routers.skbook.middlewares=default-no-crowdsec@file" in labels


# --- sticky cookies are opt-in, not a silent default change ---------------

def test_no_sticky_cookie_labels_by_default():
    doc = render()
    labels = _labels(doc, "bookstack")
    assert not any("loadbalancer.sticky" in l for l in labels)


def test_sticky_cookie_labels_opt_in():
    doc = render(sticky_enabled=True)
    labels = _labels(doc, "bookstack")
    assert "traefik.http.services.skbook.loadbalancer.sticky.cookie=true" in labels


def test_compose_still_renders_valid_yaml_with_all_hooks_set():
    doc = render(tls_options="opt@file", router_middlewares=["mw@file"], sticky_enabled=True)
    assert "deploy" in doc["services"]["bookstack"]
