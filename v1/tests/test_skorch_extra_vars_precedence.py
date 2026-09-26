"""Ansible's `-e @vars` extra-vars outrank a play's own `set_fact`, so a
`set_fact: skorch: "{{ skorch_defaults | combine(skorch) }}"` (naming the
fact after the very var it reads) gets silently discarded: the templates
then see the bare extra-vars dict, missing whatever only the defaults
supplied (POSTGRES_DB, POSTGRES_USER, ...). The fix renames the fact to
`skorch_cfg`, and every skorch template reads `skorch_cfg.*`, never
`skorch.*`, so nothing is exposed to extra-vars clobbering the combine."""
import pathlib
import re

import jinja2

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKORCH_DIR = ANSIBLE / "optional/skorch"

TEMPLATES = [
    "src/config/skorch/skorch.env.j2",
    "src/config/skorch/postgres.env.j2",
    "src/config/skorch/postgres-backup.env.j2",
    "src/config/skorch/skorch.sh.env.j2",
    "src/config/skorch/skorch.yml.j2",
]

# What deploy_skorch-dev.yml's `vars: skorch_defaults:` block declares,
# mirrored here so this test fails if the two drift.
SKORCH_DEFAULTS = {
    "N8N_HOST": "skorch-dev.cluster1.example.com",
    "N8N_PROTOCOL": "https",
    "N8N_PORT": "5678",
    "POSTGRES_DB": "skorch_db",
    "POSTGRES_USER": "skorch_user",
    "WEBHOOK_URL": "https://skorch-dev.cluster1.example.com/",
    "N8N_RUNNERS_ENABLED": "true",
    "N8N_COMMUNITY_PACKAGES_ALLOW_TOOL_USAGE": "true",
    "N8N_REINSTALL_MISSING_PACKAGES": "true",
    "GENERIC_TIMEZONE": "UTC",
    "enable_gpg_signer": False,
}

REQUIRED_SECRETS = {
    "POSTGRES_PASSWORD": "pw",
    "REDIS_PASSWORD": "redispw",
}


def combine(defaults, override):
    """python-equivalent of ansible's `combine(..., recursive=True)`."""
    out = dict(defaults)
    out.update(override)
    return out


def render_all(skorch_cfg, env_name="dev"):
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    tpl_env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("yes", "true", "1")
    rendered = {}
    for rel in TEMPLATES:
        src = (SKORCH_DIR / rel).read_text()
        rendered[rel] = tpl_env.from_string(src).render(
            app="skorch", env=env_name, skorch_cfg=skorch_cfg, cluster_name="cluster1", domain="example.com"
        )
    return rendered


def test_extra_vars_that_clobber_skorch_still_produce_complete_config():
    # Simulates `ansible-playbook ... -e @vars.yml` where vars.yml sets
    # `skorch:` with only the secrets, none of the defaulted keys -- this
    # is exactly the dict extra-vars precedence hands to `skorch_defaults |
    # combine(skorch)`, and (before the fix) then clobbers the result of
    # that combine right back to this same bare dict.
    extra_vars_skorch = dict(REQUIRED_SECRETS)  # no POSTGRES_DB / POSTGRES_USER
    skorch_cfg = combine(SKORCH_DEFAULTS, extra_vars_skorch)

    rendered = render_all(skorch_cfg)
    postgres_env = rendered["src/config/skorch/postgres.env.j2"]
    backup_env = rendered["src/config/skorch/postgres-backup.env.j2"]
    skorch_env = rendered["src/config/skorch/skorch.env.j2"]

    assert "POSTGRES_DB=skorch_db" in postgres_env
    assert "POSTGRES_USER=skorch_user" in postgres_env
    assert "POSTGRES_DB=skorch_db" in backup_env
    assert "POSTGRES_USER=skorch_user" in backup_env
    assert "DB_POSTGRESDB_DATABASE=skorch_db" in skorch_env
    assert "DB_POSTGRESDB_USER=skorch_user" in skorch_env
    # and the actual secret from the (clobbered) extra-vars dict still wins
    assert "DB_POSTGRESDB_PASSWORD=pw" in skorch_env


def test_instance_can_still_override_a_default():
    extra_vars_skorch = dict(REQUIRED_SECRETS, POSTGRES_DB="custom_db")
    skorch_cfg = combine(SKORCH_DEFAULTS, extra_vars_skorch)
    rendered = render_all(skorch_cfg)
    assert "POSTGRES_DB=custom_db" in rendered["src/config/skorch/postgres.env.j2"]


NO_BARE_SKORCH_ATTR = re.compile(r"(?<!['\"])\bskorch\.(?=[A-Za-z_])")
JINJA_SPAN = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)


def test_no_template_reads_the_shadowable_skorch_fact():
    # Regression guard for the exact bug: a template reading `skorch.KEY`
    # instead of `skorch_cfg.KEY` silently gets the un-combined dict when
    # extra-vars are in play. String literals like 'skorch.' (used to build
    # a fallback hostname) are not attribute access and are allowed, and
    # nor is plain text outside any Jinja expression/statement (e.g. the
    # literal env_file name "skorch.env").
    hits = []
    for rel in TEMPLATES:
        text = (SKORCH_DIR / rel).read_text()
        for n, line in enumerate(text.splitlines(), 1):
            for span in JINJA_SPAN.findall(line):
                if NO_BARE_SKORCH_ATTR.search(span):
                    hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "\n".join(hits)


def test_env_file_reference_was_not_accidentally_renamed():
    # The rename script's regex could, in principle, mangle the literal
    # env_file filename "skorch.env" (it parses as "skorch." + "env" too).
    # Lock the real filename down.
    yml = (SKORCH_DIR / "src/config/skorch/skorch.yml.j2").read_text()
    assert "/skorch.env" in yml
    assert "/skorch_cfg.env" not in yml
