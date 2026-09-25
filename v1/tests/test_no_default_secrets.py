"""A published framework must fail closed: a secret-looking variable may not
fall back to a literal default, or every instance that forgets to set it
runs with the same public value."""
import pathlib
import re

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SECRET = re.compile(
    r"\{\{\s*([\w.]*(secret|password|passwd|token|api_?key|private_?key)[\w.]*)\s*\|\s*default\(\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)


def test_secret_variables_have_no_literal_default():
    hits = []
    for p in ANSIBLE.rglob("*"):
        if p.is_file() and p.suffix in {".j2", ".yml", ".yaml"}:
            for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                for m in SECRET.finditer(line):
                    hits.append(f"{p.relative_to(ANSIBLE)}:{n}: {m.group(1)} defaults to a literal")
    assert not hits, "\n".join(hits)
