"""The thin CLI — what the all-in-one installer wires up as the `skwire` command."""
from __future__ import annotations

from skwire import cli, Pack, register_pack, clear_registry
import pytest


@pytest.fixture(autouse=True)
def _clean():
    clear_registry(); yield; clear_registry()


def test_scan_prints_env_and_suggestions(capsys):
    rc = cli.main(["scan"])
    out = capsys.readouterr().out.lower()
    assert rc == 0
    assert "os=" in out and ("here" in out or "deploy" in out)


def test_packs_lists_registered(capsys):
    register_pack(Pack(name="demo", nodes=[{"name": "a", "provides": {"url": "x"}}]))
    cli.main(["packs"])
    assert "demo" in capsys.readouterr().out


def test_plan_explains_registered_nodes(capsys):
    register_pack(Pack(name="demo", nodes=[
        {"name": "b", "provides": {"url": "x"}},
        {"name": "a", "needs": [{"service": "b", "secret": "k"}]},
    ]))
    cli.main(["plan"])
    out = capsys.readouterr().out.lower()
    assert "want me to" in out and "a" in out and "b" in out


def test_plan_with_no_packs_is_graceful(capsys):
    rc = cli.main(["plan"])
    assert rc != 0
    assert "pack" in capsys.readouterr().out.lower()
