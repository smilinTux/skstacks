"""sksso branding: the skworld pack and the optional-parts contract of
tasks/branding.yml. No network, no Authentik: static checks on the pack files
and on the task YAML."""
import py_compile
import re
from pathlib import Path

import yaml

ROLE = Path(__file__).resolve().parents[1] / "ansible" / "optional" / "sksso"
PACK = ROLE / "branding" / "skworld"
TASKS = ROLE / "tasks" / "branding.yml"


def test_pack_ships_media_and_apply_script():
    for name in ("skworld-logo.svg", "skworld-favicon.svg", "skworld.css", "apply_branding.py"):
        assert (PACK / "media" / name).is_file(), name
    script = PACK / "apply-branding.sh"
    assert script.is_file()
    text = script.read_text()
    assert "/media/public/skworld/apply_branding.py" in text
    assert "SKWORLD_BRAND_TITLE" in text
    assert "sksso-prod" in text


def test_pack_is_self_contained():
    assert not (PACK / "templates").exists()
    assert not (PACK / "nextcloud-theming.sh").exists()


def test_apply_branding_compiles_and_sets_brand_and_recovery(tmp_path):
    src = PACK / "media" / "apply_branding.py"
    py_compile.compile(str(src), cfile=str(tmp_path / "x.pyc"), doraise=True)
    code = src.read_text()
    assert "default-recovery-flow" in code
    for field in ("branding_title", "branding_logo", "branding_favicon"):
        assert field in code, field
    assert "flow_recovery" in code
    assert "recovery_flow" in code
    assert "flows-recovery-email-verification.yaml" in code
    for prefix in ("brand", "flow", "stage", "missing"):
        assert re.search(r'print\("%s ' % prefix, code), prefix


def _tasks():
    return yaml.safe_load(TASKS.read_text())


def test_branding_yml_templates_and_nextcloud_helper_are_optional():
    tasks = _tasks()
    stat = next(t for t in tasks if "stat" in t and "loop" in t)
    assert set(stat["loop"]) == {"templates", "nextcloud-theming.sh"}
    assert stat["register"] == "sksso_branding_optional"
    copy_tpl = next(t for t in tasks if t.get("name", "").startswith("Copy branded email templates"))
    assert "templates" in copy_tpl["when"] and ".stat.exists" in copy_tpl["when"]
    helpers = next(t for t in tasks if t.get("name", "").startswith("Install the branding helpers"))
    srcs = {i["src"] for i in helpers["loop"]}
    assert srcs == {"apply-branding.sh", "nextcloud-theming.sh"}
    assert "nextcloud-theming.sh" in helpers["when"] and ".stat.exists" in helpers["when"]
    # apply-branding itself is always installed
    assert "apply-branding.sh" not in helpers["when"].replace("item.src", "")


def test_nam_pack_still_complete():
    nam = ROLE / "branding" / "nam"
    assert (nam / "nextcloud-theming.sh").is_file()
    assert (nam / "templates" / "nam_recovery.html").is_file()


def test_no_estate_identifiers_in_pack():
    banned = re.compile(r"douno\.it|skstack01|nativeasset|\b\d{1,3}(\.\d{1,3}){3}\b", re.I)
    for f in PACK.rglob("*"):
        if f.is_file():
            assert not banned.search(f.read_text()), f
            assert "—" not in f.read_text() and "–" not in f.read_text(), f
