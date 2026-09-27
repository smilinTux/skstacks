"""Every shell script shipped under v1/ansible must at least parse.

skpulse's discover-traefik-frontends.sh had an unescaped backtick inside a
double-quoted sed expression (`[^`]*`), so bash read the rest of the file as
an unterminated command substitution: `bash -n` failed with "unexpected EOF
while looking for matching ``'" and the script died the moment it reached
extract_fqdn. Found by the real-Ansible render gate (v1/tests/render/)."""
import pathlib
import shutil
import subprocess

import pytest

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SCRIPTS = sorted(ANSIBLE.glob("*/*/src/**/*.sh"))
SKPULSE = ANSIBLE / "optional/skpulse/src/skpulse/discover-traefik-frontends.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def test_there_are_scripts_to_check():
    assert SKPULSE in SCRIPTS


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(ANSIBLE)))
def test_shell_script_parses(script):
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("rule, fqdn", [
    ("Host(`grafana.render.example.test`)", "grafana.render.example.test"),
    ("Host(`a.example.test`) && PathPrefix(`/x`)", "a.example.test"),
])
def test_skpulse_extract_fqdn(rule, fqdn):
    text = SKPULSE.read_text()
    func = text[text.index("extract_fqdn() {"):]
    func = func[:func.index("\n}\n") + 3]
    proc = subprocess.run(["bash", "-c", func + 'extract_fqdn "$1"', "_", rule],
                          capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == fqdn
