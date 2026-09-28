"""No shell body may pipe into `grep -q` while `pipefail` is set.

`grep -q` exits at the first match. If the writer is still writing, its next
write gets SIGPIPE, the writer exits 141, and under `set -o pipefail` the
whole pipeline then reports failure even though grep matched. The more
output there is to match against, the likelier that gets: a false "no
match" that shows up only on a busy cluster. skport's socket-proxy
detection had this shape (`docker service ls | grep -q ...` in an `if`), and
a misfire silently picked the wrong proxy name.

Capture the output first and match the captured text instead
(`out=$(cmd) || fail; grep -q PAT <<<"$out"`), which also lets a failing
`cmd` be reported instead of being read as "no match". Note that `grep
PAT >/dev/null` is not a fix: GNU grep treats /dev/null output like -q and
also stops at the first match.

Scope: the whole repository. Ansible shell/command task bodies are judged
one task at a time (each task is its own shell); any other file with shell
in it (scripts, templates) is judged as a whole, since a `set -o pipefail`
at the top covers every function below it.
"""
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
SKIP_SUFFIXES = {".md", ".py", ".pyc", ".json", ".png", ".jpg", ".svg", ".lock", ".txt"}
SHELL_KEYS = {"shell", "command", "ansible.builtin.shell", "ansible.builtin.command",
              "ansible.legacy.shell", "ansible.legacy.command"}

# `| grep ... -q ...` (also -qx, -Eq, --quiet, --silent), across a line
# continuation or a newline after the pipe.
PIPE_TO_GREP_Q = re.compile(
    r"\|[ \t\\\n]*grep\b[^|;&\n]*?\s(?:-[A-Za-z]*q[A-Za-z]*|--quiet|--silent)(?=\s|$)")
PIPEFAIL = re.compile(r"\bpipefail\b")


def _task_bodies(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in SHELL_KEYS:
                if isinstance(value, str):
                    yield value
                elif isinstance(value, dict) and isinstance(value.get("cmd"), str):
                    yield value["cmd"]
            else:
                yield from _task_bodies(value)
    elif isinstance(node, list):
        for item in node:
            yield from _task_bodies(item)


def find_violations(root=ROOT):
    hits = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or SKIP_DIRS & set(f.parts) or f.suffix in SKIP_SUFFIXES:
            continue
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if "grep" not in text or not PIPEFAIL.search(text):
            continue
        rel = f.relative_to(root)
        if f.suffix in (".yml", ".yaml"):
            try:
                doc = yaml.safe_load(text)
            except yaml.YAMLError:
                doc = None
            if doc is not None:
                for body in _task_bodies(doc):
                    if PIPEFAIL.search(body):
                        hits += [f"{rel}: {m.group(0).strip()!r}" for m in PIPE_TO_GREP_Q.finditer(body)]
                continue
        hits += [f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0).strip()!r}"
                 for m in PIPE_TO_GREP_Q.finditer(text)]
    return hits


def test_no_pipe_to_grep_q_under_pipefail():
    hits = find_violations()
    assert not hits, ("`| grep -q` under pipefail (SIGPIPE false negative); capture the output "
                      "first, then grep -q <<<\"$out\":\n  " + "\n  ".join(hits))


def test_detector_catches_the_shapes_it_is_meant_to(tmp_path):
    (tmp_path / "a.sh").write_text('set -euo pipefail\nif docker service ls | grep -qx "x"; then :; fi\n')
    (tmp_path / "b.sh.j2").write_text('set -o pipefail\nfoo \\\n  | grep --quiet bar\n')
    (tmp_path / "c.yml").write_text(yaml.safe_dump([{"name": "t", "shell": "set -o pipefail\nls | grep -Eq x\n"}]))
    (tmp_path / "ok.sh").write_text('set -o pipefail\nout=$(ls)\ngrep -q x <<<"$out"\nls | grep x\n')
    (tmp_path / "nopf.sh").write_text('ls | grep -q x\n')
    (tmp_path / "d.yml").write_text(yaml.safe_dump([
        {"name": "pf", "shell": "set -o pipefail\necho ok\n"},
        {"name": "other task, no pipefail", "shell": "ls | grep -q x\n"}]))
    hits = find_violations(tmp_path)
    assert sorted(h.split(":")[0] for h in hits) == ["a.sh", "b.sh.j2", "c.yml"], hits
