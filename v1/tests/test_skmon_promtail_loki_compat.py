"""Promtail 3.x pushes to a Loki 3.x server; keep the two majors in step, pinned by digest."""
import pathlib

import jinja2
import yaml

from image_pins import is_pinned

SKMON = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmon/src/config/skmon"


def _services():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters.setdefault("bool", lambda v: str(v).lower() in ("1", "true", "yes"))
    out = env.from_string((SKMON / "skmon.yml.j2").read_text()).render(app="skmon", env="prod", skmon={"CLUSTERNAME": "c", "DOMAIN": "e.com"})
    return yaml.safe_load(out)["services"]


def test_promtail_3x_pinned():
    assert is_pinned(_services()["promtail"]["image"].replace("grafana/promtail", "promtail"), "promtail", "3.")


def test_promtail_major_matches_loki_major():
    svc = _services()
    assert svc["promtail"]["image"].split(":")[1].split(".")[0] == svc["loki"]["image"].split(":")[1].split(".")[0]


def test_promtail_config_schema_unchanged():
    cfg = yaml.safe_load((SKMON / "promtail-config.yml.j2").read_text())
    assert cfg["positions"]["filename"] and cfg["clients"][0]["url"].endswith("/loki/api/v1/push")
    assert {j["job_name"] for j in cfg["scrape_configs"]} == {"system", "docker"}
