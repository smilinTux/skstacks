"""MariaDB moves to the 12.3 LTS (the newest LTS Nextcloud 35 supports; 13.x is a
rolling release and unsupported). Upstream upgrades a datadir in place across
majors when MARIADB_AUTO_UPGRADE is set, so no fail-closed guard is needed, but the
env must set it and the image must stay overridable so an instance can keep a major."""
import pathlib

import jinja2
import yaml

from image_pins import is_pinned

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional"
SKBOOK = ANSIBLE / "skbook/src/config/skbook"
SKHUB = ANSIBLE / "skhub/src/config/skhub"


def _render(path, **ctx):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters.setdefault("bool", lambda v: str(v).lower() in ("1", "true", "yes"))
    return env.from_string(path.read_text()).render(**ctx)


def _skbook_services(**skbook):
    base = {"CLUSTERNAME": "c1", "DOMAIN": "example.com"}
    return yaml.safe_load(_render(SKBOOK / "skbook.yml.j2", app="skbook", env="prod", skbook=dict(base, **skbook)))["services"]


def test_skbook_db_and_backup_default_to_12_3_pinned():
    svc = _skbook_services()
    assert is_pinned(svc["db"]["image"], "mariadb", "12.3.")
    assert is_pinned(svc["db-backup"]["image"], "mariadb", "12.3.")


def test_skbook_image_pin_keeps_an_older_major():
    svc = _skbook_services(MARIADB_IMAGE="mariadb:10.11")
    assert svc["db"]["image"] == svc["db-backup"]["image"] == "mariadb:10.11"


def test_healthchecks_use_mariadb_admin():
    svc = _skbook_services()
    assert "mariadb-admin ping" in svc["db"]["healthcheck"]["test"][1]
    assert "mysqladmin" not in _render(SKHUB / "skhub.yml.j2", app="skhub", env="prod", skhub={"CLUSTERNAME": "c", "DOMAIN": "e.com"}, fence_service_name="x")


def test_env_files_enable_auto_upgrade():
    skbook = {"MYSQL_ROOT_PASSWORD": "r", "DB_DATABASE": "d", "DB_USER": "u", "DB_PASSWORD": "p"}
    assert "MARIADB_AUTO_UPGRADE=1" in _render(SKBOOK / "skbook.env.j2", app="skbook", env="prod", skbook=skbook)
    skhub = {"mysql_root_password": "r", "mysql_user": "u", "mysql_password": "p", "DOMAIN": "e.com"}
    assert "MARIADB_AUTO_UPGRADE=1" in _render(SKHUB / "skhub.env.j2", app="skhub", env="prod", skhub=skhub)


def _skhub_services(**skhub):
    base = {"CLUSTERNAME": "c", "DOMAIN": "e.com"}
    out = _render(SKHUB / "skhub.yml.j2", app="skhub", env="prod", skhub=dict(base, **skhub), fence_service_name="x")
    return yaml.safe_load(out)["services"]


def test_db_stop_grace_period_defaults_to_120s():
    # Room for a slow shutdown on a busy NFS datadir. The tc.log crashloop itself is
    # handled by the entrypoint guard (test_mariadb_tclog_guard.py): the gate showed a
    # clean, fast shutdown can still leave the closed log behind.
    assert _skbook_services()["db"]["stop_grace_period"] == "120s"
    assert _skhub_services()["db"]["stop_grace_period"] == "120s"


def test_db_stop_grace_period_is_a_knob():
    assert _skbook_services(DB_STOP_GRACE_PERIOD="300s")["db"]["stop_grace_period"] == "300s"
    assert _skhub_services(DB_STOP_GRACE_PERIOD="5m")["db"]["stop_grace_period"] == "5m"


def test_db_stop_grace_period_empty_falls_back():
    assert _skhub_services(DB_STOP_GRACE_PERIOD="")["db"]["stop_grace_period"] == "120s"
