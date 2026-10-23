"""inventory.collect_cluster_inventory must list every running service's
pinned image, tolerate one service's inspect failing without losing the
rest, and report "unreachable" (not crash) when every manager host times
out - the same "a node hangs, not just a command fails" shape of failure
this tool has to survive unattended, running daily with nobody watching."""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "image_monitor"))

from inventory import collect_cluster_inventory  # noqa: E402


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_happy_path_lists_every_service_with_its_pinned_image():
    def fake_runner(argv, capture_output, text, timeout):
        cmd = argv[-1]
        if cmd == "true":
            return FakeCompleted(0)
        if "service ls" in cmd:
            return FakeCompleted(0, stdout="svc-a\nsvc-b\n")
        if "svc-a" in cmd:
            return FakeCompleted(0, stdout="redis:7-alpine@sha256:" + "a" * 64 + "\n")
        if "svc-b" in cmd:
            return FakeCompleted(0, stdout="nextcloud:34.0.4@sha256:" + "b" * 64 + "\n")
        raise AssertionError(cmd)

    result = collect_cluster_inventory("nor", ["mgr1"], runner=fake_runner)
    assert not result.unreachable
    assert result.manager_used == "mgr1"
    images = {s.service: s.image for s in result.services}
    assert images["svc-a"].startswith("redis:7-alpine@sha256:a")
    assert images["svc-b"].startswith("nextcloud:34.0.4@sha256:b")


def test_one_service_inspect_failure_does_not_drop_the_rest():
    def fake_runner(argv, capture_output, text, timeout):
        cmd = argv[-1]
        if cmd == "true":
            return FakeCompleted(0)
        if "service ls" in cmd:
            return FakeCompleted(0, stdout="svc-a\nsvc-removed\n")
        if "svc-removed" in cmd:
            return FakeCompleted(1, stderr="Error: no such service: svc-removed")
        if "svc-a" in cmd:
            return FakeCompleted(0, stdout="redis:7-alpine@sha256:" + "a" * 64 + "\n")
        raise AssertionError(cmd)

    result = collect_cluster_inventory("nor", ["mgr1"], runner=fake_runner)
    assert not result.unreachable
    assert [s.service for s in result.services] == ["svc-a"]


def test_every_manager_unreachable_reports_cluster_unreachable_not_a_crash():
    def fake_runner(argv, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    result = collect_cluster_inventory("nor", ["mgr1", "mgr2"], runner=fake_runner)
    assert result.unreachable
    assert result.services == []


def test_second_manager_used_when_first_is_unreachable():
    def fake_runner(argv, capture_output, text, timeout):
        if "mgr1" in argv:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
        cmd = argv[-1]
        if cmd == "true":
            return FakeCompleted(0)
        if "service ls" in cmd:
            return FakeCompleted(0, stdout="svc-a\n")
        return FakeCompleted(0, stdout="redis:7-alpine@sha256:" + "a" * 64 + "\n")

    result = collect_cluster_inventory("nor", ["mgr1", "mgr2"], runner=fake_runner)
    assert not result.unreachable
    assert result.manager_used == "mgr2"
