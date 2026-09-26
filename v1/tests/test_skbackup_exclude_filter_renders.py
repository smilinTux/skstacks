"""skbackup's default exclude list must skip its own dir and every
service's live runtime DB/cache datadir (those are covered by each stack's
own dump-to-disk sidecar, e.g. skorch's postgres-backup) -- and must be
fully instance-configurable via skbackup.exclude."""
import pathlib

import jinja2

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
EXCLUDE = ANSIBLE / "optional/skbackup/src/config/skbackup/exclude.filter.j2"


def render(env_name="prod", **skbackup_overrides):
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = tpl_env.from_string(EXCLUDE.read_text()).render(app="skbackup", env=env_name, skbackup=skbackup_overrides)
    return [l for l in out.splitlines() if l and not l.startswith("#")]


def test_default_excludes_own_dir_and_own_config_dir():
    lines = render("prod")
    assert "-/source/skbackup-prod/*" in lines
    assert "-/source/config/skbackup-prod/*" in lines


def test_default_excludes_every_service_runtime_datadir():
    lines = render("prod")
    assert "-/source/runtime/*" in lines


def test_default_excludes_generic_caches():
    lines = render("prod")
    assert "-*/.cache/*" in lines
    assert "-*/cache/*" in lines


def test_env_specific_own_dir_exclusion():
    lines = render("dev")
    assert "-/source/skbackup-dev/*" in lines
    assert "-/source/skbackup-prod/*" not in lines


def test_exclude_list_is_fully_instance_overridable():
    lines = render("prod", exclude=["-/source/only-this/*"])
    assert lines == ["-/source/only-this/*"]
