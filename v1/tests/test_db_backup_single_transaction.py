"""mariadb-dump without --single-transaction takes a read lock per database
for the whole dump. On skhub (NAM, 2026-10-16 ~13:13-13:27Z) the daily
`db-backup` service ran `mariadb-dump --all-databases` with no
--single-transaction against a ~1.5GB Nextcloud DB; the dump sat 13+ min
"Writing to net" on oc_filecache, 152 Nextcloud queries queued on "Waiting
for table metadata lock", and Nextcloud + Talk hung (status.php timeouts,
talk-hpb "context deadline exceeded", Android Talk clients failing) until
the coordinator killed the dump query by hand.

Fix: add --single-transaction (InnoDB: dumps from one consistent snapshot,
taken with a quick metadata lock instead of a long table lock) and --quick
(streams rows instead of buffering the whole result set, so a big table does
not balloon dump-process memory) to every mariadb-dump invocation. Also add
--routines --events so stored procedures/functions and the event scheduler
are captured in a --all-databases backup (previously silently omitted).

Caveat (documented, not fixed here): --single-transaction only gives a
consistent snapshot for InnoDB tables. Nextcloud's own schema is InnoDB-only
(oc_* tables), so this is safe for skhub/skbook as deployed; a MyISAM table
present in any database at dump time would not be covered by the single
transaction and would need its own lock (same caveat upstream mariadb-dump
has always had). See CHANGELOG.

skbook dumps a single database (no --all-databases); the lock-duration and
memory risk and the fix are otherwise identical.
"""
import pathlib
import re

import jinja2
import yaml

OPTIONAL = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional"

SKHUB = OPTIONAL / "skhub" / "src/config/skhub/skhub.yml.j2"
SKBOOK = OPTIONAL / "skbook" / "src/config/skbook/skbook.yml.j2"


def _render(path, **variables):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return env.from_string(path.read_text()).render(**variables)


def _skhub_doc():
    rendered = _render(SKHUB, app="skhub", env="prod",
                        skhub={"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"},
                        fence_service_name="skfenceha")
    return yaml.safe_load(rendered)


def _skbook_doc():
    rendered = _render(SKBOOK, app="skbook", env="prod", skbook={})
    return yaml.safe_load(rendered)


def _dump_line(entrypoint):
    assert isinstance(entrypoint, str)
    for line in entrypoint.splitlines():
        if "mariadb-dump" in line:
            return line
    raise AssertionError(f"no mariadb-dump line found in entrypoint:\n{entrypoint}")


def test_skhub_dump_uses_single_transaction_and_quick():
    line = _dump_line(_skhub_doc()["services"]["db-backup"]["entrypoint"])
    assert "--single-transaction" in line, line
    assert "--quick" in line, line


def test_skhub_dump_keeps_all_databases_and_gzip():
    doc = _skhub_doc()
    entrypoint = doc["services"]["db-backup"]["entrypoint"]
    line = _dump_line(entrypoint)
    assert "--all-databases" in line, line
    assert "| gzip" in line, line


def test_skhub_dump_captures_routines_and_events():
    line = _dump_line(_skhub_doc()["services"]["db-backup"]["entrypoint"])
    assert "--routines" in line, line
    assert "--events" in line, line


def test_skhub_retention_logic_unchanged():
    entrypoint = _skhub_doc()["services"]["db-backup"]["entrypoint"]
    assert "BACKUP_NUM_KEEP" in entrypoint
    assert re.search(r"ls -tr /dump/dump_\*\.sql\.gz.*head -n -.*BACKUP_NUM_KEEP", entrypoint)


def test_skbook_dump_uses_single_transaction_and_quick():
    line = _dump_line(_skbook_doc()["services"]["db-backup"]["entrypoint"])
    assert "--single-transaction" in line, line
    assert "--quick" in line, line


def test_skbook_dump_keeps_single_database_and_gzip():
    line = _dump_line(_skbook_doc()["services"]["db-backup"]["entrypoint"])
    assert "MYSQL_DATABASE" in line, line
    assert "| gzip" in line, line


def test_skbook_dump_captures_routines_and_events():
    line = _dump_line(_skbook_doc()["services"]["db-backup"]["entrypoint"])
    assert "--routines" in line, line
    assert "--events" in line, line


def test_no_mariadb_dump_invocation_is_missing_single_transaction():
    """Belt and suspenders: every mariadb-dump call in either template must
    carry --single-transaction, so a future added flag near this one can't
    quietly reintroduce the locking bug."""
    for doc in (_skhub_doc(), _skbook_doc()):
        for svc in doc["services"].values():
            entrypoint = svc.get("entrypoint")
            if isinstance(entrypoint, str) and "mariadb-dump" in entrypoint:
                for line in entrypoint.splitlines():
                    if "mariadb-dump " in line or line.strip().startswith("mariadb-dump"):
                        assert "--single-transaction" in line, line
