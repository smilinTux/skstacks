"""php_probe.probe_runtime must find the node a service's task is actually
running on and exec the probe there, but never raise: a container with no
PHP (wrong image matched a runtime's "images" patterns) or an unreachable
node must come back as a ProbeResult.error, not an exception, since one
bad probe must not take down the whole fleet run."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "image_monitor"))

from php_probe import probe_runtime  # noqa: E402


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_happy_path_finds_node_container_and_probes_it():
    def fake_runner(argv, capture_output, text, timeout):
        cmd = argv[-1]
        if "service ps" in cmd:
            return FakeCompleted(0, stdout="norap1003\n")
        if "docker ps" in cmd:
            return FakeCompleted(0, stdout="a298d0a6b4ed\n")
        if "docker exec" in cmd:
            return FakeCompleted(0, stdout="8.5.11")
        raise AssertionError(cmd)

    result = probe_runtime("skhub-prod_nextcloud", ["php", "-r", "echo PHP_VERSION;"], manager="norap1001",
                           jump=None, user=None, identity=None, runner=fake_runner)
    assert result.error is None
    assert result.output == "8.5.11"


def test_no_running_task_is_an_error_not_a_crash():
    def fake_runner(argv, capture_output, text, timeout):
        return FakeCompleted(0, stdout="")  # service ps finds nothing running

    result = probe_runtime("skbook-prod_bookstack", ["php", "-r", "echo PHP_VERSION;"], manager="norap1001",
                           jump=None, user=None, identity=None, runner=fake_runner)
    assert result.output is None
    assert "no running task" in result.error


def test_container_without_the_probed_runtime_is_an_error_not_a_crash():
    def fake_runner(argv, capture_output, text, timeout):
        cmd = argv[-1]
        if "service ps" in cmd:
            return FakeCompleted(0, stdout="namwrk1004\n")
        if "docker ps" in cmd:
            return FakeCompleted(0, stdout="7ae6fbc6f9a1\n")
        if "docker exec" in cmd:
            return FakeCompleted(126, stderr='OCI runtime exec failed: exec: "php": executable file not found')
        raise AssertionError(cmd)

    result = probe_runtime("skflow-prod_worker", ["php", "-r", "echo PHP_VERSION;"], manager="nammgr1001",
                           jump="laptop", user="namuser01", identity="~/.ssh/ansible", runner=fake_runner)
    assert result.output is None
    assert "not found" in result.error


def test_node_unreachable_is_an_error_not_a_crash():
    import subprocess

    def fake_runner(argv, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    result = probe_runtime("skhub-prod_nextcloud", ["php", "-r", "echo PHP_VERSION;"], manager="norap1001",
                           jump=None, user=None, identity=None, runner=fake_runner)
    assert result.output is None
    assert result.error
