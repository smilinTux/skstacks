"""Database dump sidecars must keep a history of good dumps, never lose one.

Found on prod (skgallery, sksso): the sidecar entrypoint was
`bash -c 'bash -s <<EOF ... EOF'` with an UNQUOTED heredoc, so the outer
shell expanded the backtick `date` exactly once, when the container started.
Every loop then wrote the same file name, `> file` truncated the only copy
before pg_dump ran (a failed dump left an empty or partial file where the
good one was), and `xargs rm -- {}` errored on every loop. skmem-pg and
skmesh carried the same body.

Required behaviour, checked here for every such sidecar:
* a unique file name per run (the date is taken inside the loop);
* the dump is written to a temp file and renamed only on success, so a
  failed dump never replaces or removes a good one;
* retention is N days (vault knob, env BACKUP_KEEP_DAYS) and prunes only
  after a successful dump, so a run of failures never ages out the last
  good copy.

The functional test renders each template, applies compose's `$$` -> `$`
interpolation, and runs the real entrypoint script under bash with fake
pg_dump / pg_dumpall / date / sleep on PATH and /dump pointed at a tmpdir.
"""
import os
import pathlib
import re
import stat
import subprocess
import time

import jinja2
import pytest
import yaml

OPTIONAL = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional"

# (compose template, service name, env template, dump suffix, vars)
SIDECARS = {
    "skgallery": (
        "skgallery/src/config/skgallery/skgallery.yml.j2",
        "postgres-db-backup",
        "skgallery/src/config/skgallery/skgallery-backup.env.j2",
        ".psql",
        lambda knobs: {"app": "skgallery", "env": "prod", "skgallery": dict(DB_PASSWORD="x", **knobs)},
        "BACKUP_KEEP_DAYS", "BACKUP_NUM_KEEP",
    ),
    "sksso": (
        "sksso/src/config/sksso/sksso.yml.j2",
        "postgres-db-backup",
        "sksso/src/config/sksso/sksso-backup.env.j2",
        ".psql",
        lambda knobs: {"app": "sksso", "env": "prod",
                       "sksso": dict(postgres_user="u", postgres_password="x", **knobs)},
        "backup_keep_days", "backup_num_keep",
    ),
    "skmem-pg": (
        "skmem-pg/src/config/skmem-pg/skmem-pg.yml.j2",
        "skmem-pg-backup",
        "skmem-pg/src/config/skmem-pg/skmem-pg-backup.env.j2",
        ".psql",
        lambda knobs: dict({"app": "skmem-pg", "env": "prod", "skmem_pg_password": "x"},
                           **{"skmem_pg_" + k: v for k, v in knobs.items()}),
        "backup_keep_days", "backup_num_keep",
    ),
    "skmesh": (
        "skmesh/src/config/skmesh/skmesh.yml.j2",
        "postgres-db-backup",
        "skmesh/src/config/skmesh/postgres-backup.env.j2",
        ".sql.gz",
        lambda knobs: {"app": "skmesh", "env": "prod", "skmesh": dict(POSTGRES_PASSWORD="x", **knobs)},
        "BACKUP_KEEP_DAYS", "BACKUP_NUM_KEEP",
    ),
}


def _render(rel, variables):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string((OPTIONAL / rel).read_text()).render(**variables)


def _entrypoint(name):
    rel, svc, _, _, mkvars, _, _ = SIDECARS[name]
    doc = yaml.safe_load(_render(rel, mkvars({})))
    return doc["services"][svc]["entrypoint"]


def _script(name):
    ep = _entrypoint(name)
    assert isinstance(ep, list) and ep[:2] == ["bash", "-c"] and len(ep) == 3, (
        f"{name}: entrypoint must be [bash, -c, <script>] so exactly one shell "
        f"expands it (no `bash -s <<EOF` heredoc), got {ep!r}")
    return ep[2]


@pytest.mark.parametrize("name", sorted(SIDECARS))
def test_no_heredoc_or_backtick_date(name):
    """Static guard for the bug class: the date must not be expanded by an
    outer shell before the loop runs."""
    script = _script(name)
    assert "<<" not in script and "`" not in script, name


@pytest.mark.parametrize("name", sorted(SIDECARS))
def test_every_dollar_is_escaped_for_compose(name):
    """docker stack deploy interpolates `${VAR}` from the DEPLOYER's shell;
    a single `$` in the script silently becomes an empty string. Every `$`
    must be `$$`."""
    stripped = _script(name).replace("$$", "")
    assert "$" not in stripped, f"{name}: unescaped $ in {stripped!r}"


def _compose_unescape(script):
    return script.replace("$$", "$")


FAKE_PG_DUMP = r"""#!/bin/bash
# fake pg_dump: honours -f FILE; fails (after a partial write) when $FAKE_FAIL exists
out=""
while [ $# -gt 0 ]; do
  case "$1" in -f) out="$2"; shift 2;; -f*) out="${1#-f}"; shift;; *) shift;; esac
done
if [ -n "$out" ]; then exec >"$out"; fi
printf 'PGDMP partial'
if [ -e "$FAKE_FAIL" ]; then exit 1; fi
head -c 4096 /dev/urandom
"""

FAKE_PG_DUMPALL = r"""#!/bin/bash
printf -- '-- partial'
if [ -e "$FAKE_FAIL" ]; then exit 1; fi
head -c 4096 /dev/urandom
"""

FAKE_DATE = r"""#!/bin/bash
# every call returns a new second, so each loop gets a distinct name
n=$(( $(cat "$FAKE_STATE/date" 2>/dev/null || echo 0) + 1 ))
echo "$n" >"$FAKE_STATE/date"
printf '01-01-2026_00_00_%02d\n' "$n"
"""

FAKE_SLEEP = r"""#!/bin/bash
# counts loop sleeps; flips to failing dumps after FAIL_AFTER good ones and
# stops the sidecar (SIGTERM to its shell) after ITERATIONS loops
case "$1" in 2m) exit 0;; esac
n=$(( $(cat "$FAKE_STATE/sleep" 2>/dev/null || echo 0) + 1 ))
echo "$n" >"$FAKE_STATE/sleep"
if [ "$n" -ge "$FAIL_AFTER" ]; then touch "$FAKE_FAIL"; fi
if [ "$n" -ge "$ITERATIONS" ]; then kill -TERM "$PPID"; fi
exit 0
"""


def _run(name, tmp_path, *, iterations, fail_after, keep_days="7", pre_existing=()):
    suffix = SIDECARS[name][3]
    dump = tmp_path / "dump"
    dump.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    for tool, body in (("pg_dump", FAKE_PG_DUMP), ("pg_dumpall", FAKE_PG_DUMPALL),
                       ("date", FAKE_DATE), ("sleep", FAKE_SLEEP)):
        p = bindir / tool
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    for fname, age_days in pre_existing:
        f = dump / fname
        f.write_text("old good dump")
        t = time.time() - age_days * 86400
        os.utime(f, (t, t))
    if fail_after == 0:
        (state / "fail").touch()
    script = _compose_unescape(_script(name)).replace("/dump", str(dump))
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_STATE=str(state),
               FAKE_FAIL=str(state / "fail"), FAIL_AFTER=str(fail_after),
               ITERATIONS=str(iterations), PGUSER="u", PGDATABASE="d", PGHOST="h",
               BACKUP_KEEP_DAYS=keep_days, BACKUP_FREQUENCY="1")
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (name, proc.returncode, proc.stderr)
    finals = sorted(p.name for p in dump.iterdir() if p.name.endswith(suffix))
    others = sorted(p.name for p in dump.iterdir() if not p.name.endswith(suffix) and p.is_file())
    return finals, others, dump


@pytest.mark.parametrize("name", sorted(SIDECARS))
def test_each_run_writes_a_new_file(name, tmp_path):
    finals, others, dump = _run(name, tmp_path, iterations=3, fail_after=99)
    assert len(finals) == 3, finals
    assert others == [], others
    for f in finals:
        assert (dump / f).stat().st_size > 1024


@pytest.mark.parametrize("name", sorted(SIDECARS))
def test_failed_dump_never_replaces_or_removes_a_good_one(name, tmp_path):
    # 2 good dumps, then 2 failing ones: the good ones stay, full size, and
    # the partial output of the failures is neither a final file nor left
    # behind as a temp file
    finals, others, dump = _run(name, tmp_path, iterations=4, fail_after=2)
    assert len(finals) == 2, finals
    for f in finals:
        assert (dump / f).stat().st_size > 1024, f
    assert others == [], f"temp/partial files left behind: {others}"


@pytest.mark.parametrize("name", sorted(SIDECARS))
def test_retention_prunes_by_age_after_a_good_dump(name, tmp_path):
    suffix = SIDECARS[name][3]
    old = f"dump_01-01-2020_00_00_00{suffix}"
    recent = f"dump_02-01-2020_00_00_00{suffix}"
    finals, _, _ = _run(name, tmp_path, iterations=1, fail_after=99,
                        keep_days="7", pre_existing=[(old, 8), (recent, 3)])
    assert old not in finals, "dump older than BACKUP_KEEP_DAYS must be pruned"
    assert recent in finals, "dump inside BACKUP_KEEP_DAYS must be kept"
    assert len(finals) == 2


@pytest.mark.parametrize("name", sorted(SIDECARS))
def test_failures_do_not_age_out_the_last_good_dump(name, tmp_path):
    suffix = SIDECARS[name][3]
    old = f"dump_01-01-2020_00_00_00{suffix}"
    finals, _, _ = _run(name, tmp_path, iterations=3, fail_after=0,
                        keep_days="1", pre_existing=[(old, 30)])
    assert finals == [old]


@pytest.mark.parametrize("name", sorted(SIDECARS))
def test_retention_is_a_vault_knob_with_num_keep_fallback(name):
    _, _, env_rel, _, mkvars, days_key, num_key = SIDECARS[name]

    def keep_days(knobs):
        out = _render(env_rel, mkvars(knobs))
        m = re.search(r"^BACKUP_KEEP_DAYS=(\S+)$", out, re.M)
        assert m, out
        return m.group(1)

    assert keep_days({}) == "7"
    assert keep_days({num_key: "5"}) == "5"  # existing vault key keeps its meaning (1 dump a day)
    assert keep_days({days_key: "14", num_key: "5"}) == "14"

