"""Live-parity hook: skorch (n8n) on skstack01 sets N8N_MCP_SERVER_ENABLED/
HOST/PATH/PORT (confirmed via docker service inspect skorch-prod_n8n, names
only), which skorch.env.j2 had no place for. skorch_cfg.extra_env closes the
gap generically, matching the pattern already used for sksso/skpeek."""
import pathlib

import jinja2

SKORCH_ENV = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skorch/src/config/skorch/skorch.env.j2"

REQUIRED = {"POSTGRES_DB": "d", "POSTGRES_USER": "u", "POSTGRES_PASSWORD": "p", "REDIS_PASSWORD": "r"}


def render(**skorch_cfg):
    j2 = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    cfg = dict(REQUIRED)
    cfg.update(skorch_cfg)
    return j2.from_string(SKORCH_ENV.read_text()).render(skorch_cfg=cfg, cluster_name="cluster1", domain="example.com")


def test_extra_env_absent_by_default():
    out = render()
    assert "N8N_MCP_SERVER_ENABLED=" not in out


def test_extra_env_closes_the_mcp_server_gap():
    out = render(extra_env={
        "N8N_MCP_SERVER_ENABLED": "true",
        "N8N_MCP_SERVER_HOST": "0.0.0.0",
        "N8N_MCP_SERVER_PATH": "/mcp",
        "N8N_MCP_SERVER_PORT": "5679",
    })
    assert "N8N_MCP_SERVER_ENABLED=true" in out
    assert "N8N_MCP_SERVER_HOST=0.0.0.0" in out
    assert "N8N_MCP_SERVER_PATH=/mcp" in out
    assert "N8N_MCP_SERVER_PORT=5679" in out
