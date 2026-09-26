"""Every v1 *.env.j2 must render one KEY=value per line under Ansible's
template settings (trim_blocks=True, keep_trailing_newline=True).

Generic guard for the newline-eating class found in skhub.env.j2: a
`{%- set -%}` / `{%- if -%}` that strips the newline on both sides, or an
inline `{% endif %}` at end of line that trim_blocks turns into "join with
the next line". Either one silently glues two variables together
(`DEFAULT_PHONE_REGION=USNEXTCLOUD_CUSTOM_APPS=...`), and Docker's env_file
parser takes the result verbatim with no error.

Each template is rendered twice for dev and prod: once with every var
undefined (all defaults) and once with every var set to a truthy stand-in
(so the optional `{% if %}` branches render too)."""
import pathlib
import re

import jinja2
import jinja2.meta
import pytest

V1 = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES = sorted(V1.glob("ansible/**/*.env.j2"))
JOINED = re.compile(r"[A-Z_]+=.*[A-Z][A-Z0-9_]{2,}=")


class Anything(str):
    """Truthy stand-in for any instance var: a string ("val") whose every
    attribute/item is itself, so `ns.a.b`, `ns.a + '.x'` and `| lower` all
    work without knowing each template's vars."""

    def __new__(cls):
        return super().__new__(cls, "val")

    def __getattr__(self, name):
        return self

    def __getitem__(self, key):
        return self

    def __iter__(self):
        return iter(())

    def __call__(self, *args, **kwargs):
        return self


def _tpl_env():
    env = jinja2.Environment(
        undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True
    )
    env.filters["bool"] = lambda v: str(v).lower() in ("1", "true", "yes", "on", "val")
    return env


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: str(p.relative_to(V1)))
@pytest.mark.parametrize("env_name", ["dev", "prod"])
@pytest.mark.parametrize("mode", ["defaults", "all-set"])
def test_no_two_assignments_on_one_line(path, env_name, mode):
    tpl_env = _tpl_env()
    src = path.read_text()
    names = jinja2.meta.find_undeclared_variables(tpl_env.parse(src)) - {"env"}
    ctx = {"env": env_name}
    namespaces = []
    for n in names:
        if mode == "all-set":
            ctx[n] = Anything()
        elif re.search(rf"\b{n}\s*[.\[]", src):
            ctx[n] = {}  # namespace dict: every key undefined -> defaults
            namespaces.append(ctx[n])
        else:
            ctx[n] = "val"  # plain top-level var such as cluster_name
    tpl = tpl_env.from_string(src)
    for _ in range(50):
        try:
            out = tpl.render(**ctx)
            break
        except jinja2.UndefinedError as exc:
            # a var the template requires (no default): supply it, keep
            # every optional one on its default, and retry
            m = re.search(r"has no attribute '(\w+)'", str(exc))
            assert m and namespaces, exc
            for ns in namespaces:
                ns.setdefault(m.group(1), "val")
    else:
        pytest.fail("could not satisfy required vars")
    joined = [l for l in out.splitlines() if not l.lstrip().startswith("#") and JOINED.search(l)]
    assert not joined, joined
