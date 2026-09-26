"""A Jinja type test (`is mapping`, `is string`, ...) on an optional instance
key fails under Ansible when the key is absent: Ansible's templar raises on
the undefined attribute before the test runs ("object of type 'dict' has no
attribute 'GZIP_COMPRESS'", skstack06 v2.19.0 rc3 preflight). Plain jinja2
render tests (ChainableUndefined) never see it. Every `<ns>.<KEY> is <type>`
must be guarded by `<ns>.<KEY> is defined and` or read through `| default(`."""
import pathlib
import re

import pytest

V1 = pathlib.Path(__file__).resolve().parents[1] / "ansible"
TEMPLATES = sorted(V1.glob("*/*/src/**/*.j2"))
TYPE_TEST = re.compile(r"\b([a-z_][a-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s+is\s+(?:not\s+)?(mapping|string|sequence|number|iterable|boolean|integer|float)\b")


def _violations(text):
    bad = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in TYPE_TEST.finditer(line):
            ref = m.group(1)
            before = line[: m.start()]
            if f"{ref} is defined" in before or f"{ref} | default" in line or f"{ref}|default" in line:
                continue
            bad.append(f"{n}: {line.strip()}")
    return bad


def test_templates_found():
    assert len(TEMPLATES) > 100


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: str(p.relative_to(V1)))
def test_type_tests_on_optional_keys_are_guarded(path):
    assert _violations(path.read_text()) == []
