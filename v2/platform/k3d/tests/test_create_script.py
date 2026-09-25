"""create.sh must call k3d the way k3d v5 accepts: the cluster name is a
positional argument (`--name` is rejected: "unknown flag: --name")."""
import os
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "create.sh"


def test_cluster_name_is_positional(tmp_path):
    log = tmp_path / "calls"
    for tool in ("k3d", "kubectl"):
        stub = tmp_path / tool
        stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> {log}\n')
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "K3D_CONFIG": "ci", "K3D_CLUSTER_NAME": "t1", "HOME": str(tmp_path)}
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    create = next(l for l in log.read_text().splitlines() if l.startswith("k3d cluster create"))
    assert "--name" not in create
    assert create.split()[3] == "t1"
