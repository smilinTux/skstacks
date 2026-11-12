"""skmon role README (Task 23): required sections, a vault table that matches what the code reads,
the migration runbook, and no estate identifiers."""
import re

from skmon_support import ROLE, services

README = (ROLE / "README.md").read_text()

SECTIONS = ["## Vault keys", "## Install modes", "## Data node and storage", "## Alert routing",
            "## Dead-man's switch", "## Parking", "## Sizing", "## skhealth stays",
            "## Migration from skmon v1", "## Verify", "## Backups"]

SPEC_12_KEYS = {"DATA_NODE", "DATA_PATH", "METRICS_RETENTION", "LOGS_RETENTION", "PROFILES_RETENTION",
                "SCRAPE_INTERVAL_FAST", "SCRAPE_INTERVAL", "AGENT_PORT", "AGENT_LISTEN_ADDR", "PROFILING",
                "PVE", "PROBES", "ALERT_ROUTING", "ALERT_SECRETS", "D_STATE_THRESHOLD", "ESTATE",
                "skmon_extra_hosts"}
PLAN_KEYS = {"HEALTHZ_PORT", "HEALTHZ_ALLOW", "SKALERT_SCRIPT_SRC", "SKALERT_SCRIPT_SHA256",
             "ALERT_OUT_OF_DOMAIN_CHANNEL", "ALERT_ACCEPT_IN_DOMAIN_ONLY", "ALERT_ACCEPT_IN_DOMAIN_ONLY_REASON",
             "GRAFANA_OIDC_CLIENT_ID", "GRAFANA_OIDC_CLIENT_SECRET", "AUTHENTIK_PROVISION", "PVE_TOKEN_ID",
             "PVE_TOKEN_SECRET", "BRIDGE_UID", "LOKI_PUSH_PORT", "PYROSCOPE_PORT", "FORWARD_AUTH_MIDDLEWARE"}
RETIRED_KEYS = {"AUTHENTIK_TILE_GROUP", "GRAFANA_BASIC_AUTH"}


def section(heading: str) -> str:
    start = README.index(f"\n{heading}\n") + len(heading) + 2
    nxt = re.search(r"^## ", README[start:], re.M)
    return README[start:start + nxt.start()] if nxt else README[start:]


def code_keys() -> set[str]:
    """Every skmon.<KEY> or skmon['KEY'] the role's templates, tasks, plays and plugins read."""
    keys: set[str] = set()
    for p in ROLE.rglob("*"):
        if p.suffix not in (".yml", ".j2", ".py", ".sh") or "__pycache__" in p.parts:
            continue
        for m in re.finditer(r"\bskmon(?:\.|\[')([A-Za-z_0-9]+)", p.read_text(errors="replace")):
            keys.add(m.group(1))
    return {k for k in keys if k.isupper() or k in ("replicas", "networks")}


def test_readme_sections():
    for h in SECTIONS:
        assert f"\n{h}\n" in README, h


def test_every_spec_12_key_documented():
    vault = section("## Vault keys")
    for k in SPEC_12_KEYS | PLAN_KEYS:
        assert f"`{k}`" in vault, k


def test_every_key_the_code_reads_is_documented():
    vault = section("## Vault keys")
    missing = sorted(k for k in code_keys() if f"`{k}`" not in vault)
    assert not missing, missing


def test_retired_keys_not_in_vault_table():
    vault = section("## Vault keys")
    assert not [k for k in RETIRED_KEYS if k in vault]
    assert re.search(r"retired", section("## Removed keys")) and all(k in section("## Removed keys") for k in RETIRED_KEYS)


def test_runbook_retired_list_matches_code():
    retired = {"prometheus", "node-exporter", "cadvisor", "promtail", "jaeger"}
    assert retired.isdisjoint(services()) and all(f"`{r}`" in section("## Migration from skmon v1") for r in retired)


def test_runbook_has_seven_steps_and_the_done_rule():
    s = section("## Migration from skmon v1")
    assert len(re.findall(r"^\d+\. ", s, re.M)) == 7 and "outside probe is green" in s and "amtool alert add" in s


def test_sizing_formula_present():
    assert "nodes x 1,500" in section("## Sizing") and "plan 15 GB" in section("## Sizing")


def test_skhealth_and_backups_content():
    assert "skhealth" in section("## skhealth stays") and "backstop" in section("## skhealth stays")
    b = section("## Backups")
    assert "`DATA_PATH/grafana`" in b and "`DATA_PATH/alertmanager`" in b


def test_readme_has_no_estate_identifiers_or_long_dashes():
    assert not re.search(r"\b(\d{1,3}\.){3}\d{1,3}\b", README)
    assert chr(0x2014) not in README and chr(0x2013) not in README
