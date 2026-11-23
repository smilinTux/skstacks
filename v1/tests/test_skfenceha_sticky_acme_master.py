"""The skfenceha deploy keeps the traefik.acme.master label where it is.

It used to remove the label from every manager and put it on whichever
manager this run selected (the first one that answered). A deploy that
selected another manager moved traefik-acme with it, and skha, which renders
keepalived priorities from the same label, stayed on the old node until its
next deploy: the next skha check showed every priority changing. Now the label
is sticky: exactly one Ready and Active holder keeps it; it moves only when no
manager has it, more than one has it, or the holder is down or drained;
skfenceha.ACME_MASTER_NODE pins it.

The task's shell is rendered from the real playbooks and run against a fake
`docker` that keeps labels and node state in files.
"""
import pathlib
import shlex
import subprocess

import jinja2
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skfenceha"
TASK = "Keep or place the ACME master label (sticky)"
MANAGERS = ["mgr1", "mgr2", "mgr3"]

FAKE_DOCKER = r"""#!/bin/sh
# fake docker: node inspect / node update against files in $FAKE_STATE
d=$FAKE_STATE
[ "$1" = node ] || exit 1
case "$2" in
  inspect)
    n=$3; fmt=$5
    [ -f "$d/state.$n" ] || exit 1
    case "$fmt" in
      *Labels*) cat "$d/label.$n" 2>/dev/null; echo;;
      *Status*) cat "$d/state.$n";;
    esac;;
  update)
    echo "$*" >> "$d/updates"
    case "$3" in
      --label-rm) rm -f "$d/label.$5";;
      --label-add) printf true > "$d/label.$5";;
    esac;;
esac
"""


def _task(env):
    plays = yaml.safe_load((ROOT / f"deploy_skfenceha-{env}.yml").read_text())
    for play in plays:
        for t in play.get("tasks", []) or []:
            if t.get("name") == TASK:
                return t
    raise AssertionError(f"{TASK!r} not found in deploy_skfenceha-{env}.yml")


def _render(env, selected, pin=""):
    j = jinja2.Environment(undefined=jinja2.StrictUndefined)
    j.filters["quote"] = lambda s: shlex.quote(str(s))
    return j.from_string(_task(env)["shell"]).render(
        skfenceha={"ACME_MASTER_NODE": pin},
        selected_manager=selected,
        target_manager_group="managers",
        groups={"managers": MANAGERS},
    )


def _run(tmp_path, env="prod", selected="mgr1", pin="", labeled=(), down=()):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    for n in MANAGERS:
        (state / f"state.{n}").write_text("down active" if n in down else "ready active")
    for n in labeled:
        (state / f"label.{n}").write_text("true")
    r = subprocess.run(
        ["sh", "-c", _render(env, selected, pin)],
        capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_STATE": str(state)},
    )
    holders = sorted(n for n in MANAGERS if (state / f"label.{n}").exists())
    return r, holders


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_single_ready_holder_keeps_the_label(tmp_path, env):
    r, holders = _run(tmp_path, env=env, selected="mgr1", labeled=["mgr3"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert holders == ["mgr3"]
    assert "(kept)" in r.stdout
    assert "CHANGED" not in r.stdout
    assert not (tmp_path / "state" / "updates").exists()


def test_no_holder_places_it_on_the_selected_manager(tmp_path):
    r, holders = _run(tmp_path, selected="mgr2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert holders == ["mgr2"]
    assert "CHANGED" in r.stdout


def test_two_holders_collapse_to_the_selected_manager(tmp_path):
    r, holders = _run(tmp_path, selected="mgr1", labeled=["mgr2", "mgr3"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert holders == ["mgr1"]


def test_holder_that_is_down_moves_to_the_selected_manager(tmp_path):
    r, holders = _run(tmp_path, selected="mgr1", labeled=["mgr3"], down=["mgr3"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert holders == ["mgr1"]
    assert "moved" in r.stdout


def test_pin_wins_over_a_ready_holder(tmp_path):
    r, holders = _run(tmp_path, selected="mgr1", pin="mgr2", labeled=["mgr3"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert holders == ["mgr2"]
    assert "(pinned)" in r.stdout


def test_pin_outside_the_group_fails_and_changes_nothing(tmp_path):
    r, holders = _run(tmp_path, selected="mgr1", pin="other", labeled=["mgr3"])
    assert r.returncode == 3
    assert holders == ["mgr3"]
    assert _task("prod")["failed_when"] == "acme_label_result.rc | default(0) != 0"


def test_old_move_every_run_tasks_are_gone():
    for env in ("dev", "staging", "prod"):
        text = (ROOT / f"deploy_skfenceha-{env}.yml").read_text()
        assert "Remove ACME master label from all manager nodes" not in text
        assert "Label the selected manager as ACME master" not in text
