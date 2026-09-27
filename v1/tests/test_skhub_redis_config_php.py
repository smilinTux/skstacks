"""Nextcloud config.php assigns $CONFIG and returns nothing, so
`$config = include $config_file;` yields the scalar 1 and the Redis step died
with "Cannot use a scalar value as an array" (hidden by ignore_errors until
#66, then fatal on skstack06 v2.19.0 rc4). The step must include the file and
read $CONFIG."""
import pathlib

import pytest

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
PLAYBOOKS = sorted(SKHUB.glob("deploy_skhub-*.yml"))


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_config_php_is_read_through_CONFIG(playbook):
    text = playbook.read_text()
    assert "= include \\$config_file" not in text
    assert "include \\$config_file;" in text and "\\$config = \\$CONFIG;" in text
