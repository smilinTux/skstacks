"""skpatch patched images: the swap in a rendered stack file must replace
only exact matches of the upstream reference a patched image was built
from, keep everything else on the line and in the file, honour the skip
list, and be wired in front of the pre-pull so every pre-pulling stack
gets it."""
import importlib.util
import json
import pathlib
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "v1/ansible/shared/src/image_overrides.py"
spec = importlib.util.spec_from_file_location("image_overrides", SCRIPT)
io = importlib.util.module_from_spec(spec)
spec.loader.exec_module(io)

UP = "louislam/uptime-kuma:2.5.12"
PATCHED = "ghcr.io/smilintux/skpatched:louislam.uptime-kuma-2.5.12-skp0123456789ab@sha256:" + "a" * 64
STACK = f"""services:
  kuma:
    image: {UP}
    environment:
      X: "image: {UP}"
  other:
    image: "louislam/uptime-kuma:2.5.11"
  quoted:
    image: '{UP}'   # pinned
"""


def test_exact_match_is_swapped_and_the_rest_is_untouched():
    out, replaced = io.apply(STACK, {UP: {"image": PATCHED}})
    assert [r["line"] for r in replaced] == [3, 9]
    assert f"    image: {PATCHED}\n" in out
    assert f"    image: '{PATCHED}'   # pinned\n" in out
    assert '    image: "louislam/uptime-kuma:2.5.11"' in out
    assert f'      X: "image: {UP}"' in out
    assert len(out.splitlines()) == len(STACK.splitlines())


def test_a_different_digest_is_not_swapped():
    pinned = UP + "@sha256:" + "b" * 64
    out, replaced = io.apply(f"services:\n  k:\n    image: {pinned}\n", {UP: PATCHED})
    assert replaced == [] and pinned in out


def test_docker_hub_spellings_match():
    for ref in ("docker.io/" + UP, "index.docker.io/" + UP):
        _, replaced = io.apply(f"services:\n  k:\n    image: {ref}\n", {UP: PATCHED})
        assert len(replaced) == 1, ref
    _, replaced = io.apply("services:\n  p:\n    image: docker.io/library/postgres:16.16\n",
                           {"postgres:16.16": "x/y:z"})
    assert len(replaced) == 1


def test_skip_list_wins():
    out, replaced = io.apply(STACK, {UP: PATCHED}, skip=["louislam/uptime-kuma"])
    assert replaced == [] and out == STACK


def test_cli_round_trip():
    req = {"content": STACK, "overrides": {UP: {"image": PATCHED}}, "skip": []}
    res = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(req),
                         capture_output=True, text=True, check=True)
    assert len(json.loads(res.stdout)["replaced"]) == 2


def test_mapping_file_is_valid():
    data = yaml.safe_load((ROOT / "v1/ansible/shared/vars/skpatch_patched_images.yml").read_text())
    m = data["skpatch_patched_images"]
    assert isinstance(m, dict)
    for up, entry in m.items():
        assert "@sha256:" in entry["image"], up
        assert entry["image"].startswith("ghcr.io/smilintux/skpatched:"), up


def test_swap_runs_before_the_pre_pull():
    tasks = yaml.safe_load((ROOT / "v1/ansible/shared/tasks/prepull_images.yml").read_text())
    assert tasks[0]["include_tasks"] == "skpatch_image_overrides.yml"
    assert (ROOT / "v1/ansible/shared/tasks/skpatch_image_overrides.yml").exists()
