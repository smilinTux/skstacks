"""skmon must let an instance add full Prometheus scrape_configs (DNS service
discovery, relabelling), not just static targets: the estate scrapes Traefik
with dns_sd_configs, which EXTRA_SCRAPE_TARGETS cannot express."""
import pathlib

import jinja2
import yaml

TPL = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmon/src/config/skmon/prometheus.yml.j2"


def _render(**skmon):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["to_nice_yaml"] = lambda v, indent=2, **k: yaml.dump(v, indent=indent, default_flow_style=False, sort_keys=False)
    return yaml.safe_load(env.from_string(TPL.read_text()).render(app="skmon", env="dev", skmon=skmon))


def test_extra_scrape_configs_pass_through():
    job = {"job_name": "edge", "dns_sd_configs": [{"names": ["tasks.edge"], "type": "A", "port": 8082}],
           "relabel_configs": [{"source_labels": ["__meta_dns_name"], "target_label": "svc"}]}
    doc = _render(EXTRA_SCRAPE_CONFIGS=[job])
    got = next(j for j in doc["scrape_configs"] if j["job_name"] == "edge")
    assert got == job


def test_no_extras_still_valid():
    doc = _render()
    assert any(j["job_name"] == "prometheus" for j in doc["scrape_configs"])
