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

from skbackup_support import (DEFAULT_BRAND_PATTERNS, WHITE_LABEL, FakeHost, ansible_render, full_vault,
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
    assert any("etc/systemd/system/skbackup-sync.service" in f for f in found)
    assert any("usr/local/sbin/skbackup" in f for f in found)


def test_engine_scripts_carry_no_brand_literal():
    """The engine is shipped verbatim to every instance, so it may only
    print brand strings it reads from the rendered config."""
    engine = Path(__file__).resolve().parents[1] / "ansible/optional/skbackup/src/skbackup"
    texts = {str(p.relative_to(engine)): p.read_text() for p in engine.rglob("*") if p.is_file()}
    assert texts
    assert leaks(texts) == []


def engine_outputs(host: FakeHost) -> dict[str, str]:
    """Run every command a customer (or their alerts) would see output from."""
    outs = {}
    host.sanoid()
    steps = [("help",), ("--version",), ("predeploy", "upgrade"), ("predeploy", "--list"), ("sync",),
             ("status",), ("check", "--notify")]
    if shutil.which("restic"):
        steps[4:4] = [("restic", "init"), ("restic", "backup"), ("restic", "snapshots"),
                      ("restic", "restore-test"), ("restic", "forget-prune")]
    steps.append(("frobnicate",))
    steps.append(("restic", "bogus"))
    for args in steps:
        proc = host.run(*args)
        outs[" ".join(args) + " stdout"] = proc.stdout
        outs[" ".join(args) + " stderr"] = proc.stderr
    host.set_pool("tank", cap="95")          # force findings so alerts are produced
    host.run("check", "--notify")
    outs["alerts.log"] = host.alerts()
    for p in (host.state / "reports").glob("*"):
        outs["report " + p.name] = p.read_text()
    for p in host.state.rglob("*"):
        outs["state path " + str(p.relative_to(host.root))] = ""
    for p in (host.root / "lock").iterdir():
        outs["lock path " + p.name] = ""
    for ds in ("tank/data", "backup/copies/app1"):
        for n in host.snaps(ds):
            outs[f"snapshot {ds}@{n}"] = ""
    return outs


def test_engine_output_under_white_label_leaks_nothing(tmp_path):
    host = FakeHost(tmp_path, brand=dict(WHITE_LABEL), UNIT_BASE="acme-acmevault").seed()
    outs = engine_outputs(host)
    # it did print and alert, in the customer's brand
    assert outs["help stdout"].startswith("Acme Vault\n")
    assert "ACME-VAULT" in outs["alerts.log"]
    assert "Acme IT managed backup service" in outs["alerts.log"]
    if shutil.which("restic"):
        assert "RESULT: PASS" in outs["restic restore-test stdout"]
    assert leaks(outs) == []


def test_engine_output_scan_fires_on_default_brand(tmp_path):
    host = FakeHost(tmp_path).seed()
    outs = engine_outputs(host)
    assert any("SKBackup" in f for f in leaks(outs))
    assert any("skbackup" in f for f in leaks(outs))
