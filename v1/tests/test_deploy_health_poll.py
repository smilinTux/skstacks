"""Every deploy.j2 replica health check must poll for convergence instead of
checking once, ~10s after `docker stack deploy`: an update that restarts the
task (any change to the service spec) shows 0/1 for a while, so a single
check either fails the deploy or prints a misleading warning.

This generalizes test_skstor_deploy_waits.py (the original fix,
release/v2.18.0 commit 4a19bdf) to every deploy.j2 under v1/ansible with a
`docker service ls --filter ... --format ...Replicas...` health check: it
renders each template, pulls out the poll-loop + its if/.../fi, and runs
that snippet against a fake `docker` that reports 0/1 for the first three
`service ls` calls and 1/1 from the fourth on (mirroring a task that takes
~15-20s to reschedule). A script that only checks once takes the
failure/warning branch on attempt one and never recovers; a script that
polls (up to ~3 min, 5s interval, matching skstor's fix) waits it out.

Confirmed RED pre-fix: reconstructing the single-check form of each of these
blocks (assignment immediately followed by the if, with no poll loop) and
running it through this same harness fails 36 of the 37 matching blocks in
the repo's pre-fix state. The one exception, skdesk, already retries via its
own (shorter) loop before this pattern and is out of scope here.
"""
import os
import pathlib
import re
import stat
import subprocess

import jinja2
import pytest

V1_DIR = pathlib.Path(__file__).resolve().parents[1]
DEPLOY_FILES = sorted((V1_DIR / "ansible").glob("*/*/src/*/deploy.j2"))

# Matches the poll loop this fix introduces (see skstor's deploy.j2).
POLL_RE = re.compile(r"for _ in \$\(seq 1 36\); do\n.*?\n[ \t]*done", re.DOTALL)
IF_START_RE = re.compile(r"^([ \t]*)if .*; then[ \t]*$")
_OPENERS = ("if", "for", "while", "case")
_CLOSERS = {"fi", "done", "esac"}

FAKE_DOCKER = r"""#!/bin/bash
case "$*" in
  "service ls"*)
    n=$(cat "$STATE" 2>/dev/null || echo 0); echo $((n+1)) > "$STATE"
    if [ "$n" -ge 3 ]; then echo "1/1"; else echo "0/1"; fi ;;
  "service logs"*)
    echo called >> "$FAILLOG" ;;
  *) : ;;
esac
"""


def _render(path: pathlib.Path) -> str:
    app = path.parent.name
    tmpl = jinja2.Environment(undefined=jinja2.ChainableUndefined).from_string(path.read_text())
    return tmpl.render(env="dev", app=app, **{app: {}})


def _extract_blocks(text: str) -> list:
    """Pull out every `poll loop` + its immediately following `if ... fi`
    (or `if ... done`/`esac`, tracking nesting) out of a rendered deploy
    script. Intervening non-blank lines (e.g. a sibling assignment) before
    the `if` are tolerated, within a short lookahead."""
    blocks = []
    for m in POLL_RE.finditer(text):
        loop_text = m.group(0)
        lines = text[m.end():].splitlines(keepends=True)
        idx = 0
        while idx < len(lines) and not IF_START_RE.match(lines[idx]) and idx < 5:
            idx += 1
        if idx >= len(lines) or not IF_START_RE.match(lines[idx]):
            continue
        depth = 1
        end_idx = idx + 1
        while end_idx < len(lines) and depth > 0:
            stripped = lines[end_idx].strip()
            first_word = stripped.split(" ", 1)[0].split(";", 1)[0] if stripped else ""
            if first_word in _OPENERS:
                depth += 1
            elif stripped in _CLOSERS or first_word in _CLOSERS:
                depth -= 1
            end_idx += 1
        blocks.append(loop_text + "\n" + "".join(lines[idx:end_idx]))
    return blocks


def _discover_cases():
    cases = []
    for path in DEPLOY_FILES:
        rel = path.relative_to(V1_DIR)
        for i, block in enumerate(_extract_blocks(_render(path))):
            cases.append(pytest.param(block, id=f"{rel}[{i}]"))
    return cases


CASES = _discover_cases()


def test_discovered_expected_health_blocks():
    # Sanity floor: guards against the extraction regex silently matching
    # nothing after a future change to the poll-loop style.
    assert len(CASES) >= 26


@pytest.mark.parametrize("block", CASES)
def test_health_check_waits_for_convergence(tmp_path, block):
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    script = (
        'GREEN=""; YELLOW=""; RED=""; NC=""; BLUE=""; CYAN=""\n'
        # Generic stand-ins for names/counts these blocks reference but
        # don't themselves assign (e.g. skgallery's per-service EXPECTED).
        # The fake docker ignores filter values, so only vars that feed a
        # success comparison (like EXPECTED) actually matter here.
        'STACK_NAME=svc-dev; EXPECTED=1\n'
        'sleep() { :; }\n'
        # Wrapped in a function so a bare `return` (e.g. skgallery's
        # check_service_health helper) is valid outside its own file, and
        # so its exit status is a second, independent success signal
        # alongside "did it call `docker service logs`".
        '_test_block() {\n' + block + '\n}\n'
        '_test_block\n'
        'echo "BLOCK_RC=$?"\n'
    )
    env = dict(
        os.environ,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        STATE=str(tmp_path / "n"),
        FAILLOG=str(tmp_path / "faillog"),
    )
    r = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (tmp_path / "faillog").exists(), (
        "health check took the failure/warning branch (called `docker service "
        "logs`) instead of waiting for convergence:\n" + r.stdout + r.stderr
    )
    block_rc = re.search(r"BLOCK_RC=(\d+)", r.stdout)
    assert block_rc and block_rc.group(1) == "0", (
        "health check function returned non-zero instead of converging:\n"
        + r.stdout + r.stderr
    )
