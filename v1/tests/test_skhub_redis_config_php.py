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


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_config_php_is_written_with_real_newlines(playbook):
    """PHP single quotes do not interpret \\n: `\x27<?php\\n$CONFIG = \x27` wrote a
    literal backslash-n, config.php became `<?php\\n$CONFIG = array (...` and
    Nextcloud died with "syntax error, unexpected variable $CONFIG" (skstack06
    v2.19.1 fresh run). The header/trailer must use PHP_EOL."""
    line = next(l for l in playbook.read_text().splitlines() if "file_put_contents(\\$config_file" in l)
    assert "\x27<?php\\n" not in line and "\x27;\\n\x27" not in line
    assert "PHP_EOL" in line
