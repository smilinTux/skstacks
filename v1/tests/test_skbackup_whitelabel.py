"""White-label mode: an instance that sets its own skbackup.branding (an MSP
deploying for a customer) must ship with NO default brand visible in
anything the customer sees on the storage host: rendered units, config, CLI
wrapper, the engine scripts the deploy copies, the shell the deploy runs,
the install paths themselves, and everything the engine prints or sends
(banner, help, status, alerts, reports).

The scan is proven to work by running it on the default brand, where it
must find the strings."""
import re
import shutil
from pathlib import Path

import pytest

from skbackup_support import (DEFAULT_BRAND_PATTERNS, WHITE_LABEL, Engine, ansible_render, full_vault,
                              needs_ansible, rendered)

LEAK = re.compile("|".join(re.escape(p) for p in DEFAULT_BRAND_PATTERNS), re.I)


def leaks(named_texts: dict[str, str]) -> list[str]:
    found = []
    for name, text in named_texts.items():
        for m in LEAK.finditer(name):
            found.append(f"path {name}: {m.group(0)!r}")
        for n, line in enumerate(text.splitlines(), 1):
            for m in LEAK.finditer(line):
                found.append(f"{name}:{n}: {m.group(0)!r} in {line.strip()[:100]!r}")
    return found


@needs_ansible
def test_white_label_render_leaks_no_default_brand(tmp_path):
    res, out = ansible_render(tmp_path, full_vault(branding=WHITE_LABEL, unit_prefix="acme-"))
    assert res["status"] == "PASS", res["findings"]
    files = rendered(out)
    assert any(p.startswith("_copy/usr/local/lib/acmevault/") for p in files)
    assert any(p.startswith("etc/systemd/system/") for p in files)
    assert any(p.startswith("_shell/") for p in files)
    assert leaks(files) == []


@needs_ansible
def test_the_scan_finds_the_default_brand(tmp_path):
    """Sabotage check: the same scan over the default render must fire."""
    res, out = ansible_render(tmp_path, full_vault())
    assert res["status"] == "PASS", res["findings"]
    found = leaks(rendered(out))
    assert any("etc/systemd/system/skbackup-run.service" in f for f in found)
    assert any("usr/local/sbin/skbackup" in f for f in found)


def test_engine_scripts_carry_no_brand_literal():
    """The engine is shipped verbatim to every instance, so it may only
    print brand strings it reads from the rendered config."""
    engine = Path(__file__).resolve().parents[1] / "ansible/optional/skbackup/src/skbackup"
    texts = {str(p.relative_to(engine)): p.read_text() for p in engine.rglob("*") if p.is_file()}
    assert texts
    assert leaks(texts) == []


def engine_outputs(eng: Engine, day: int) -> dict[str, str]:
    outs = {}
    t0 = 1_800_000_000
    for args, now in ((("help",), None), (("--version",), None), (("run",), t0), (("status",), t0),
                      (("predeploy", "upgrade"), t0), (("check",), t0 + day * 3)):
        proc = eng.run(*args, now=now)
        outs[" ".join(args) + " stdout"] = proc.stdout
        outs[" ".join(args) + " stderr"] = proc.stderr
    if shutil.which("restic"):
        for args in (("offsite",), ("restore-test",), ("prune",)):
            proc = eng.run(*args, now=t0)
            outs[" ".join(args) + " stdout"] = proc.stdout
            outs[" ".join(args) + " stderr"] = proc.stderr
    outs["alerts.log"] = eng.alerts()
    for p in (eng.state / "reports").glob("*"):
        outs["report " + p.name] = p.read_text()
    for p in eng.state.rglob("*"):
        outs["state path " + str(p.relative_to(eng.root))] = ""
    return outs


def test_engine_output_under_white_label_leaks_nothing(tmp_path):
    brand = dict(WHITE_LABEL)
    eng = Engine(tmp_path, brand=brand, UNIT_BASE="acme-acmevault",
                 OFFSITE_ENABLED=bool(shutil.which("restic")), RESTORE_TEST_ENABLED=bool(shutil.which("restic")),
                 DUMPS_ENABLED=True, DUMP_CHECKS=["app1|dump/*.sql|26"])
    eng.seed()
    (eng.data / "app1" / "dump").mkdir()
    (eng.data / "app1" / "dump" / "db.sql").write_text("-- dump\n")
    if shutil.which("restic"):
        eng.run("init-offsite", check=True)
    outs = engine_outputs(eng, day=86400)
    # it did print and alert, in the customer's brand
    assert "Acme Vault" in outs["help stdout"]
    assert "ACME-VAULT" in outs["alerts.log"]
    assert "Acme IT managed backup service" in outs["alerts.log"]
    assert leaks(outs) == []


def test_engine_output_scan_fires_on_default_brand(tmp_path):
    eng = Engine(tmp_path).seed()
    outs = engine_outputs(eng, day=86400)
    assert any("SKBackup" in f for f in leaks(outs))
