"""The shared header playbook must run from a plain framework checkout: no
git-meta file and no helper script (instances have neither)."""
import pathlib
import subprocess

HEADER = pathlib.Path(__file__).resolve().parents[1] / "ansible" / "shared" / "meta" / "skstack-header.yml"


def test_header_runs_without_git_meta_file(tmp_path):
    assert not (HEADER.parent / "skstack-git-meta.txt").exists()
    r = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(HEADER)],
                       cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
