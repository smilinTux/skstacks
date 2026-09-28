"""No deploy may walk a whole data tree to (re)set ownership or modes.

2026-09-27: a skgit deploy spent 40+ minutes in `chown -R` + `chmod -R 755`
over a 60G Forgejo data dir (repositories, LFS) on NFS. Everything was
already correct, so it was pure metadata I/O, and it saturated NFS for every
other service on it. GNU `chown -R` / `chmod -R` issue one SETATTR per entry
whether or not anything changes, and so does an ansible `file` task with
`recurse: yes` walking the tree; the bill scales with the data, not with what
is wrong.

The rule, for every v1 playbook, template and deploy script under
v1/ansible (comment lines ignored):

* No `chown -R`, `chmod -R`, `chgrp -R`, `--recursive`, and no ansible
  `recurse: yes`. Fix the directories that need it, or use `find` below.
* A `find ... -exec chmod|chown|chgrp` must be incremental: it selects only
  entries that are wrong (`-perm`, `-user`, `-group`, `-uid`, `-gid`,
  `-nouser`, `-nogroup`), so entries that are already right are never
  rewritten (their ctime stays put, and nothing goes over the wire).
* It must also be bounded (`-maxdepth` or `-prune`), so bulk data subtrees
  (repos, LFS, attachments, mailboxes, chunks) are not even walked, unless
  the line is on ALLOWED below with the reason a full walk of that tree is
  cheap or necessary. A stale ALLOWED entry fails too.
"""
import fnmatch
import pathlib
import re

import pytest

V1 = pathlib.Path(__file__).resolve().parents[1]
ANSIBLE = V1 / "ansible"
SUFFIXES = {".yml", ".yaml", ".j2", ".sh", ""}

RECURSIVE = re.compile(r"\b(?:chown|chmod|chgrp)\b(?:\s+-[A-Za-z]+)*\s+-[A-Za-z]*R|\b(?:chown|chmod|chgrp)\b[^\n;|&]*--recursive")
RECURSE_KEY = re.compile(r"\brecurse\s*[:=]\s*[\"']?(?:yes|true|True|1)\b")
FIND_EXEC = re.compile(r"\bfind\b.*-exec(?:dir)?\s+(?:chown|chmod|chgrp)\b")
SELECTS = re.compile(r"(?:^|\s)!?\s*-(?:perm|user|group|uid|gid|nouser|nogroup)\b")
BOUNDED = re.compile(r"-maxdepth\b|-prune\b")

# (file glob relative to v1/ansible, substring of the offending line): reason
ALLOWED = {
    ("optional/skgit/deploy_skgit-*.yml", 'find "$C" \\( ! -user'):
        "config dir only (app.ini, a handful of files), never the data dir",
    ("optional/skmesh/deploy_skmesh-*.yml", "find \"$S\" \\( ! -user"):
        "NetBird management/signal/relay state: a store db, GeoLite files and"
        " IdP data, tens of files; incremental, so a no-op run writes nothing",
    ("optional/skmesh/deploy_skmesh-*.yml", 'find "$M" \\( -perm'):
        "same small NetBird management dir: modes, incremental",
    ("optional/sksync/deploy_sksync-*.yml", 'find "$C" \\( ! -user'):
        "syncthing_config only (config.xml, keys, index db); never sync-data",
    ("optional/skmail/src/config/skmail/user-patches.sh.j2", "find /var/lib/amavis/"):
        "amavis state (db, tmp) inside the container; the quarantine"
        " (virusmails) is pruned, which amavis writes as itself",
}


def _files():
    for p in sorted(ANSIBLE.rglob("*")):
        if p.is_file() and p.suffix in SUFFIXES and "tests" not in p.relative_to(ANSIBLE).parts:
            yield p


def _logical_lines(text):
    """Code lines with shell `\\` continuations joined, comments dropped."""
    buf = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not buf and line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        yield buf + line
        buf = ""
    if buf:
        yield buf


def _allowed(rel, line):
    return next(
        (key for key in ALLOWED if fnmatch.fnmatch(rel, key[0]) and key[1] in line),
        None,
    )


def findings():
    """[(rel, problem, line, allow_key)] across v1/ansible."""
    out = []
    for p in _files():
        rel = str(p.relative_to(ANSIBLE))
        for line in _logical_lines(p.read_text(errors="replace")):
            if RECURSIVE.search(line) or RECURSE_KEY.search(line):
                out.append((rel, "recursive chown/chmod", line, None))
            elif FIND_EXEC.search(line):
                if not SELECTS.search(line):
                    out.append((rel, "find -exec rewrites entries that are already right", line, None))
                elif not BOUNDED.search(line):
                    out.append((rel, "find walks the whole tree", line, _allowed(rel, line)))
    return out


def test_no_unbounded_recursive_chown_or_chmod():
    bad = [f"{rel}: {problem}: {line}" for rel, problem, line, key in findings() if key is None]
    assert not bad, "\n".join(bad)


def test_every_allowlist_entry_is_used():
    used = {key for *_, key in findings() if key}
    assert set(ALLOWED) - used == set()


@pytest.mark.parametrize(
    "line",
    [
        "chown -R 1000:1000 /var/data/x/data || true",
        "chmod -R u+rwX,go-rwx \"$M\"",
        "docker exec c chown -hR 5000:5000 /var/mail",
        "chmod --recursive 755 /x",
        "file: { path: /x, state: directory, owner: 1, recurse: yes }",
        "recurse: true",
        r'find "$D" -type f -exec chmod 644 {} \;',
        'find "$D" \\( ! -user 1 -o ! -group 1 \\) -exec chown -h 1:1 {} +',
    ],
)
def test_detector_flags(line, tmp_path, monkeypatch):
    f = tmp_path / "optional/svc/deploy_svc-prod.yml"
    f.parent.mkdir(parents=True)
    f.write_text("# chown -R in a comment is fine\n" + line + "\n")
    monkeypatch.setattr(__import__(__name__), "ANSIBLE", tmp_path)
    assert [x for x in findings() if x[3] is None], line


@pytest.mark.parametrize(
    "line",
    [
        "# never `chmod -R 755` here",
        "chown 1000:1000 /var/data/x/data",
        'find "$D" -maxdepth 2 \\( ! -user 1 -o ! -group 1 \\) -exec chown -h 1:1 {} +',
        'find "$D" -path "$D/repos" -prune -o -perm /o+rwx -exec chmod o-rwx {} +',
        'find "$K" -type f ! -name "*.pub" ! -perm 0600 -maxdepth 1 \\\n  -exec chmod 0600 {} +',
    ],
)
def test_detector_passes(line, tmp_path, monkeypatch):
    f = tmp_path / "optional/svc/deploy_svc-prod.yml"
    f.parent.mkdir(parents=True)
    f.write_text(line + "\n")
    monkeypatch.setattr(__import__(__name__), "ANSIBLE", tmp_path)
    assert findings() == [], line
