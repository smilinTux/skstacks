"""sksso's OIDC client blueprints (sksec-web, skpatch-hosts, skflow) must set
grant_types. Authentik 2026.2+ gives a NEW OAuth2 provider an empty
grant_types list unless one is set, and then every sign-in fails with
invalid_request ("Invalid grant_type for provider"); providers that existed
before the upgrade were migrated to every grant type, so this only shows on
a fresh install. Happy path per blueprint, plus a failure check that the
code flow is in the list (a list without it would still break sign-in)."""
from pathlib import Path

import pytest

BP = Path(__file__).resolve().parents[1] / "ansible/optional/sksso/src/config/sksso/blueprints"


@pytest.mark.parametrize("name", ["sksec-web-oidc.yaml.j2", "skpatch-hosts-oidc.yaml.j2", "skflow-windmill.yaml.j2"])
def test_oidc_blueprint_sets_code_flow_grant_types(name):
    text = (BP / name).read_text()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("grant_types:")]
    assert lines == ["grant_types: [authorization_code, refresh_token]"], lines


def test_every_oauth2_blueprint_sets_grant_types():
    for path in sorted(BP.glob("*.j2")):
        text = path.read_text()
        if "authentik_providers_oauth2.oauth2provider" in text:
            assert "grant_types:" in text, f"{path.name}: oauth2provider without grant_types"
