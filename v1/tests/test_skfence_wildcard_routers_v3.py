"""The skfence/skfenceha wildcard catch-all routers must use Traefik v3 rule
syntax.

Both templates rendered ``HostRegexp(`{subdomain:.+}.<domain>`)``, the Traefik
v2 named-placeholder form. The pinned image is traefik:v3.x with no
``defaultRuleSyntax``/``ruleSyntax`` set, so v3 treats the whole string as a
Go regexp that never matches a real host: the routers were dead, and unknown
subdomains got Traefik's bare 404 instead of the branded error page.

v3 form: an anchored regexp with the domain's dots escaped.

Priority: Traefik treats ``priority: 0`` as "use the rule length", and the v3
regexp is longer than a typical service ``Host(...)`` rule. Once the routers
actually match, priority 0 would let the catch-all outrank real service
routers, so the wildcards carry an explicit low priority instead.
"""
import pathlib
import re

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
CASES = [
    ("skfence", ANSIBLE / "core/skfence/src/config/skfence/routers.yml.j2"),
    ("skfenceha", ANSIBLE / "core/skfenceha/src/config/skfenceha/routers.yml.j2"),
]
WILDCARDS = {"wildcard-domain": "example.test", "wildcard-cluster": "c1.example.test"}
_V2_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*:[^}]*\}")


def render(path, key, envname="prod", acme=False):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(path.read_text()).render(
        env=envname, domain="example.test", cluster_name="c1",
        **{key: {"ACME_ENABLED": acme}})
    return yaml.safe_load(out)["http"]["routers"]


def _regexp(rule):
    m = re.fullmatch(r"HostRegexp\(`(.+)`\)", rule)
    assert m, f"not a single HostRegexp rule: {rule}"
    return m.group(1)


@pytest.mark.parametrize("key,path", CASES)
def test_no_v2_named_placeholders_in_the_template(key, path):
    # every backtick-quoted argument on a `rule:` line, jinja expressions removed
    rules = [l for l in path.read_text().splitlines() if l.strip().startswith("rule:")]
    args = [re.sub(r"\{\{.*?\}\}", "", a) for l in rules for a in re.findall(r"`([^`]*)`", l)]
    assert args, "no rule arguments found"
    bad = [a for a in args if _V2_PLACEHOLDER.search(a)]
    assert not bad, bad


@pytest.mark.parametrize("key,path", CASES)
@pytest.mark.parametrize("name,suffix", WILDCARDS.items())
def test_wildcard_rule_is_an_anchored_escaped_v3_regexp(key, path, name, suffix):
    rx = _regexp(render(path, key)[name]["rule"])
    assert "{" not in rx and "}" not in rx
    assert rx.startswith("^") and rx.endswith("$")
    assert rx.endswith(re.escape("." + suffix) + "$"), rx
    pat = re.compile(rx)
    assert pat.match(f"anything.{suffix}")
    assert pat.match(f"a-b-9.{suffix}")
    assert not pat.match(suffix), "the bare domain belongs to root-https"
    # unescaped dots would let an unrelated domain through
    assert not pat.match(f"anything.{suffix.replace('.', 'x')}")
    assert not pat.match(f"anything.{suffix}.evil.test")


@pytest.mark.parametrize("key,path", CASES)
def test_cluster_subdomain_matches_the_cluster_wildcard(key, path):
    routers = render(path, key)
    host = "svc.c1.example.test"
    assert re.match(_regexp(routers["wildcard-cluster"]["rule"]), host)


@pytest.mark.parametrize("key,path", CASES)
@pytest.mark.parametrize("name", WILDCARDS)
def test_wildcard_priority_is_explicit_and_lowest(key, path, name):
    routers = render(path, key)
    prio = routers[name]["priority"]
    # 0 means "rule length" in Traefik, which would outrank service routers
    assert isinstance(prio, int) and prio >= 1
    others = [r.get("priority") for n, r in routers.items() if n not in WILDCARDS]
    assert all(p is None or p >= prio for p in others)


@pytest.mark.parametrize("key,path", CASES)
@pytest.mark.parametrize("envname", ["dev", "staging", "prod"])
@pytest.mark.parametrize("acme", [False, True])
def test_template_renders_to_valid_yaml_in_every_mode(key, path, envname, acme):
    routers = render(path, key, envname=envname, acme=acme)
    for name in WILDCARDS:
        _regexp(routers[name]["rule"])


def test_no_v2_placeholder_rules_anywhere_in_the_repo():
    """Any other Traefik rule template in v1 or v2 must not use v2 {name:re}."""
    repo = ANSIBLE.parents[1]
    rule_call = re.compile(r"(?:Host|HostRegexp|Path|PathPrefix|PathRegexp)\(`([^`]*)`\)")
    bad = []
    for p in list((repo / "v1").rglob("*")) + list((repo / "v2").rglob("*")):
        if not p.is_file() or p.suffix not in {".j2", ".yml", ".yaml", ".toml", ".py", ".json"}:
            continue
        if p.name == pathlib.Path(__file__).name:
            continue
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for m in rule_call.finditer(text):
            arg = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", "", m.group(1))
            if _V2_PLACEHOLDER.search(arg):
                bad.append(f"{p.relative_to(repo)}: {m.group(0)}")
    assert not bad, bad
