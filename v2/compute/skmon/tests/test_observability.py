"""Guard tests for skmon (VictoriaLogs not Loki) + skpulse (Gatus not Uptime-Kuma)."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_SKMON = Path(__file__).resolve().parents[1]
_SKPULSE = _SKMON.parent / "skpulse"


def _images(doc):
    return " ".join(s.get("image", "") for s in doc["services"].values())


def test_skmon_swarm_uses_victorialogs_not_loki():
    img = _images(yaml.safe_load((_SKMON / "swarm-compose.yml").read_text()))
    assert "victoria-logs" in img
    assert "loki" not in img
    assert "prom/prometheus" in img and "grafana/grafana" in img


def test_skmon_k8s_charts_victorialogs_not_loki():
    charts = [d["spec"]["chart"] for d in yaml.safe_load_all((_SKMON / "k8s-helmchart.yaml").read_text()) if d]
    assert "victoria-logs-single" in charts
    # no loki chart is actually deployed (comment mentions are fine)
    assert not any("loki" in c for c in charts)


def test_skmon_grafana_password_is_secret_not_inline():
    doc = yaml.safe_load((_SKMON / "swarm-compose.yml").read_text())
    assert doc["secrets"]["grafana_admin_password"]["external"] is True


def test_skpulse_is_gatus_config_as_code():
    doc = yaml.safe_load((_SKPULSE / "swarm-compose.yml").read_text())
    assert "gatus" in _images(doc)
    cfg = yaml.safe_load((_SKPULSE / "gatus-config.yaml").read_text())
    assert "endpoints" in cfg and cfg["endpoints"]


def test_skpulse_alerts_feed_skops_itil():
    cfg = yaml.safe_load((_SKPULSE / "gatus-config.yaml").read_text())
    assert "itil" in cfg["alerting"]["custom"]["url"].lower()


def test_descriptors_updated():
    assert "VictoriaLogs" in yaml.safe_load((_SKMON / "app.yaml").read_text())["provider"]
    assert "Gatus" in yaml.safe_load((_SKPULSE / "app.yaml").read_text())["provider"]
