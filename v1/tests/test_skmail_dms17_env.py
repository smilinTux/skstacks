"""docker-mailserver 17 removed or renamed several settings (16.0.0: SA_SPAM_SUBJECT,
dhparams.pem; 17.0.0: ENABLE_RSPAMD_REDIS, LDAP_QUERY_FILTER_*). The skmail templates
must not set any of them: a removed ENV is silently ignored (or worse, misread), and
this test keeps a future edit from reintroducing one."""
import pathlib
import re

SKMAIL = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmail"
TEMPLATES = list((SKMAIL / "src").rglob("*.j2"))
REMOVED = [
    r"\bSA_SPAM_SUBJECT\b",
    r"\bENABLE_RSPAMD_REDIS\b",
    r"\bLDAP_QUERY_FILTER_(USER|GROUP|ALIAS|DOMAIN|SENDERS)\b",
    r"dhparams\.pem",
    r"\bdovecot-fts-xapian\b",
]


def test_templates_exist():
    assert TEMPLATES


def test_no_removed_dms_settings_in_templates():
    hits = []
    for t in TEMPLATES:
        text = t.read_text()
        for pat in REMOVED:
            if re.search(pat, text):
                hits.append((t.name, pat))
    assert not hits, hits


def test_readme_documents_default_and_17_upgrade():
    readme = (SKMAIL / "README.md").read_text()
    assert 'DMS_VERSION: "17.0.0"' in readme
    assert "Upgrading to docker-mailserver 17" in readme
    assert "ENABLE_RSPAMD_VALKEY" in readme
