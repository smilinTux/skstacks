"""tasks/patch_sksw_config_yaml.py: patches ONLY the 5 keys sksec-web's role
owns in crowdsec-web-ui's config.yaml, round-tripping through ruamel.yaml so
comments and any other key survive untouched, and fails closed (non-zero
exit) rather than guessing when the existing file cannot be parsed at all.

Found live 2026-10-28 (see tasks/patch_app_config_yaml.yml's own header):
env vars that used to drive these 5 settings are read ONLY on first boot;
every later deploy silently ignores them, so READ_ONLY's "delete blocked"
guarantee stopped actually working after the very first deploy. These
tests run the real script via subprocess (not an import) so SKSW_CFG_B64
etc. are exercised exactly as the ansible task's `environment:` block
passes them.
"""
import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("ruamel.yaml")

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "v1" / "ansible" / "optional" / "sksec-web" / "tasks" / "patch_sksw_config_yaml.py"

BASE_CONFIG = """\
ui:
  readOnly: true
  timeZone: UTC
crowdsec:
  sync:
    lookback: 168h
instances:
  - id: default
    name: CrowdSec
    lapi:
      url: http://sksec-dev_crowdsec:8080
      auth:
        username: sksec-web
"""


def _run(cfg_text, out_file, **env_overrides):
    env = dict(os.environ)
    env.update({
        "SKSW_CFG_B64": base64.b64encode(cfg_text.encode()).decode(),
        "SKSW_READ_ONLY": "true",
        "SKSW_TIMEZONE": "UTC",
        "SKSW_LOOKBACK": "168h",
        "SKSW_LAPI_URL": "http://sksec-dev_crowdsec:8080",
        "SKSW_LAPI_USER": "sksec-web",
        "SKSW_OUT_FILE": str(out_file),
    })
    env.update(env_overrides)
    return subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, timeout=30)


def test_already_matching_reports_unchanged_and_writes_nothing(tmp_path):
    out = tmp_path / "out.yaml"
    result = _run(BASE_CONFIG, out)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "UNCHANGED"
    assert not out.exists()


def test_readonly_flip_true_to_false_changes_only_that_key(tmp_path):
    out = tmp_path / "out.yaml"
    result = _run(BASE_CONFIG, out, SKSW_READ_ONLY="false")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHANGED"
    text = out.read_text()
    assert "readOnly: false" in text
    assert "timeZone: UTC" in text
    assert "lookback: 168h" in text
    assert "url: http://sksec-dev_crowdsec:8080" in text
    assert "username: sksec-web" in text
    # untouched instances entries must round-trip (id: default still present)
    assert "id: default" in text


def test_readonly_flip_false_to_true_reverse_direction(tmp_path):
    out = tmp_path / "out.yaml"
    false_base = BASE_CONFIG.replace("readOnly: true", "readOnly: false", 1)
    result = _run(false_base, out, SKSW_READ_ONLY="true")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHANGED"
    assert "readOnly: true" in out.read_text()


def test_unparseable_yaml_fails_closed(tmp_path):
    out = tmp_path / "out.yaml"
    result = _run("ui:\n  readOnly: true\n  bad: [unterminated\n", out)
    assert result.returncode != 0
    assert "could not be parsed" in result.stderr
    assert not out.exists()


def test_empty_file_is_not_a_parse_failure(tmp_path):
    """An empty (but valid) config.yaml parses to None, not an error --
    the script must treat that as 'no owned keys reachable yet', not fail
    closed. (The instances[0] path is unreachable from an empty doc, so
    nothing is written; env-var first-boot seeding already covers a
    genuinely fresh file -- this is defense in depth, not the main path.)"""
    out = tmp_path / "out.yaml"
    result = _run("", out)
    assert result.returncode == 0, result.stderr


def test_missing_instances_array_is_skipped_not_invented(tmp_path):
    """If instances[0] does not exist, this task is not the one responsible
    for inventing a whole new instances array -- it must skip that path
    (not crash, not fabricate structure) while still patching the keys it
    can reach."""
    out = tmp_path / "out.yaml"
    cfg = "ui:\n  readOnly: true\n  timeZone: UTC\ncrowdsec:\n  sync:\n    lookback: 168h\n"
    result = _run(cfg, out, SKSW_READ_ONLY="false")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHANGED"
    assert "readOnly: false" in out.read_text()


def test_comments_and_unowned_keys_round_trip(tmp_path):
    out = tmp_path / "out.yaml"
    cfg = BASE_CONFIG + "# an operator's own comment\nextra:\n  untouchedByThisRole: yes\n"
    result = _run(cfg, out, SKSW_READ_ONLY="false")
    assert result.returncode == 0, result.stderr
    text = out.read_text()
    assert "an operator's own comment" in text
    assert "untouchedByThisRole: yes" in text
