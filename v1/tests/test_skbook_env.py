"""skbook.env.j2 (the MariaDB container's env file) was missing TZ and
EXPORT_PAGE_SIZE, both present in live skbook-prod's rendered skbook.env
(harmless there, but "present in live" -> must be present, matching hash,
in the framework render, per the same rule bookstack.env is held to)."""
import pathlib

import jinja2

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKBOOK_ENV = ANSIBLE / "optional/skbook/src/config/skbook/skbook.env.j2"


def render(**skbook_overrides):
    skbook = {
        "MYSQL_ROOT_PASSWORD": "root",
        "DB_DATABASE": "db",
        "DB_USER": "user",
        "DB_PASSWORD": "pass",
    }
    skbook.update(skbook_overrides)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string(SKBOOK_ENV.read_text()).render(app="skbook", env="prod", skbook=skbook)


def test_tz_rendered_with_default():
    out = render()
    assert "TZ=UTC" in out


def test_tz_override():
    out = render(TZ="America/New_York")
    assert "TZ=America/New_York" in out


def test_export_page_size_rendered_with_default():
    out = render()
    assert "EXPORT_PAGE_SIZE=letter" in out


def test_export_page_size_override():
    out = render(EXPORT_PAGE_SIZE="a4")
    assert "EXPORT_PAGE_SIZE=a4" in out
