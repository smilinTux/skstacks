"""skmail's docker-mailserver healthcheck must outlast DMS's own startup.

At every start DMS runs `chown -R` over each account's mailbox
(`helpers/accounts.sh:_create_accounts`, unconditional in 16.0.1, no env
toggle) plus `_chown_var_mail` over /var/mail. On NFS that is one SETATTR
round trip per file: a production start with 22 mailboxes (~2.5k files)
over a loaded NFS server took about 17 minutes. The healthcheck allowed
60s start_period + 3 x 30s retries, so Swarm killed the task as unhealthy
before postfix ever listened, restarted it, and the new task started the
same chown from the top: a kill loop and a mail outage.

skmail.HEALTHCHECK_START_PERIOD (default 1800s) sets start_period. Probe
failures inside start_period are not counted, and the first passing probe
marks the container healthy at once, so a fast start is not delayed; the
only cost is that a container that never comes up is replaced later.
"""
import pathlib
import re

import jinja2
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
COMPOSE = ANSIBLE / "optional/skmail/src/config/skmail/skmail.yml.j2"
README = ANSIBLE / "optional/skmail/README.md"
BASE = {"CLUSTERNAME": "demo", "DOMAIN": "example.test"}
OBSERVED_WORST_START_SECONDS = 17 * 60


def _healthcheck(**overrides):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(COMPOSE.read_text()).render(
        env="prod", skmail=dict(BASE, **overrides), fence_service_name="skfenceha")
    return yaml.safe_load(out)["services"]["skmail"]["healthcheck"]


def _seconds(value):
    m = re.fullmatch(r"(\d+)s", str(value))
    assert m, f"start_period {value!r} is not a plain seconds value"
    return int(m.group(1))


def test_default_start_period_outlasts_the_observed_slow_start_with_margin():
    hc = _healthcheck()
    assert hc["start_period"] == "1800s"
    assert _seconds(hc["start_period"]) >= 1.5 * OBSERVED_WORST_START_SECONDS


def test_start_period_is_an_instance_knob():
    assert _healthcheck(HEALTHCHECK_START_PERIOD="3600s")["start_period"] == "3600s"


def test_probe_cadence_is_unchanged():
    hc = _healthcheck()
    assert (hc["interval"], hc["timeout"], hc["retries"]) == ("30s", "10s", 3)


def test_readme_documents_the_knob_and_why():
    text = README.read_text()
    assert "HEALTHCHECK_START_PERIOD" in text
    assert "chown" in text
