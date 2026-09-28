"""The sksso worker healthcheck must fail when no Authentik worker runs.

The old check looked for any command line containing both "celery" and
"worker" as substrings. Its own command line (`python3 -c "...celery...
worker..."`, and the `sh -c` that CMD-SHELL wraps it in) contains both, so
it always matched itself and passed whether or not a worker was running
(prod: "healthy" on Authentik 2025.12.3, which has no celery at all). The
check now matches argv elements exactly: a process with a `worker` argument
and either a celery executable (Authentik up to 2025.8) or `manage`
(2025.10+, `python -m manage worker`).

The test runs the rendered healthcheck's own Python against a fake /proc
that also holds the checker's own processes."""
import pathlib
import re
import subprocess
import sys

import jinja2
import pytest
import yaml

SKSSO = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/sksso"


def worker_check():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string((SKSSO / "src/config/sksso/sksso.yml.j2").read_text()).render(
        env="dev", app="sksso", sksso={"CLUSTERNAME": "c", "DOMAIN": "example.com"})
    test = yaml.safe_load(out)["services"]["worker"]["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL", test
    m = re.match(r'^python3 -c "(.*)" \|\| exit 1$', test[1], re.S)
    assert m, test[1]
    return test[1], m.group(1)


def run_check(tmp_path, procs):
    shell, code = worker_check()
    root = tmp_path / "proc"
    # the checker itself and the sh -c that CMD-SHELL runs it in
    procs = {**procs, 90: ["/bin/sh", "-c", shell], 91: ["python3", "-c", code]}
    for pid, argv in procs.items():
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "cmdline").write_text("\0".join(argv) + "\0")
    (root / "self").mkdir(parents=True)  # non-numeric entries are skipped
    patched = code.replace("'/proc", f"'{root}")
    assert str(root) in patched, "healthcheck no longer reads /proc"
    return subprocess.run([sys.executable, "-c", patched]).returncode


INIT = ["dumb-init", "--", "ak", "worker"]


@pytest.mark.parametrize("worker", [
    ["/ak-root/venv/bin/python", "/ak-root/venv/bin/celery", "-A", "authentik.root.celery", "worker"],
    ["python", "-m", "manage", "worker", "--pid-file", "/dev/shm//authentik-worker.pid"],
])
def test_healthy_worker_passes(tmp_path, worker):
    assert run_check(tmp_path, {1: INIT, 7: worker}) == 0


@pytest.mark.parametrize("others", [
    {},
    {9: ["python", "-m", "manage", "migrate"]},
    {9: ["/ak-root/venv/bin/celery", "-A", "authentik.root.celery", "beat"]},
])
def test_no_worker_process_fails(tmp_path, others):
    assert run_check(tmp_path, {**others, 1: INIT}) == 1
