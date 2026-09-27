"""skmail's SMTP relay password was stored world-readable at rest.

user-patches.sh.j2 inlined `skmail.RELAY_USER` and `skmail.RELAY_PASSWORD` into
the script, which the deploy writes 0755 on the shared /var/data, and
skmail.env (which carries RELAY_PASSWORD for the container's env_file) was
rendered 0644. Anyone able to read the share could lift the relay credential.

The script now reads the relay settings from the container environment
(docker-mailserver passes env_file vars into the container, where
user-patches.sh runs), so it holds no secret and may stay 0755, and every
secret-bearing skmail template is rendered without world or group access.
"""
import os
import pathlib
import re
import stat
import subprocess

import jinja2
import pytest
import yaml

SKMAIL = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmail"
CONF = SKMAIL / "src/config/skmail"
TPL = CONF / "user-patches.sh.j2"
PLAYBOOKS = sorted(SKMAIL.glob("deploy_skmail-*.yml"))

FAKE_PASSWORD = "FAKE-relay-pass-SENTINEL-7f3a"
FAKE_USER = "FAKE-relay-user-SENTINEL-91c2"
# A template that interpolates a secret value (not one that only tests `is defined`).
SECRET_REF = re.compile(r"\{\{[^}]*skmail\.\w*(PASSWORD|SECRET|TOKEN|API_KEY)\b[^}]*\}\}")


def render(relay=True):
    skmail = {"DOMAIN": "example.test"}
    if relay:
        skmail.update(
            RELAY_HOST="smtp.relay.example.test",
            RELAY_PORT="587",
            RELAY_USER=FAKE_USER,
            RELAY_PASSWORD=FAKE_PASSWORD,
        )
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string(TPL.read_text()).render(skmail=skmail, env="dev", fence_service_name="skfenceha")


def test_rendered_script_never_contains_the_relay_credentials():
    out = render()
    assert FAKE_PASSWORD not in out
    assert FAKE_USER not in out


def test_relay_block_reads_credentials_from_the_environment():
    out = render()
    assert "SMTP RELAY CONFIGURATION" in out
    assert re.search(r"\$\{?RELAY_PASSWORD\b", out)
    assert re.search(r"\$\{?RELAY_USER\b", out)


def test_no_relay_block_without_relay_host():
    assert "RELAY_PASSWORD" not in render(relay=False)


@pytest.mark.parametrize("relay", [True, False])
def test_rendered_script_is_valid_bash(tmp_path, relay):
    script = tmp_path / "user-patches.sh"
    script.write_text(render(relay))
    subprocess.run(["bash", "-n", str(script)], check=True)


def _relay_section(tmp_path):
    """The relay-file part of the rendered script, pointed at tmp_path."""
    out = render()
    start = out.index("# SMTP RELAY CONFIGURATION")
    end = out.index("# Ensure Postfix is configured for SASL")
    body = out[start:end].replace("/tmp/docker-mailserver", str(tmp_path))
    script = tmp_path / "relay.sh"
    script.write_text("#!/bin/bash\n" + body)
    return script


def _run(script, extra_env):
    env = {"PATH": os.environ["PATH"], **extra_env}
    return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=True)


def test_relay_section_writes_maps_from_env_once(tmp_path):
    script = _relay_section(tmp_path)
    relay_env = {
        "RELAY_HOST": "smtp.relay.example.test",
        "RELAY_PORT": "2525",
        "RELAY_USER": FAKE_USER,
        "RELAY_PASSWORD": FAKE_PASSWORD,
    }
    _run(script, relay_env)
    _run(script, relay_env)
    relaymap = (tmp_path / "postfix-relaymap.cf").read_text().splitlines()
    sasl_file = tmp_path / "postfix-sasl-password.cf"
    sasl = sasl_file.read_text().splitlines()
    assert relaymap == ["@example.test [smtp.relay.example.test]:2525"]
    assert sasl == [
        f"@example.test {FAKE_USER}:{FAKE_PASSWORD}",
        f"[smtp.relay.example.test]:2525 {FAKE_USER}:{FAKE_PASSWORD}",
    ]
    assert stat.S_IMODE(sasl_file.stat().st_mode) == 0o600


def test_relay_section_skips_when_credentials_unset(tmp_path):
    script = _relay_section(tmp_path)
    res = _run(script, {"RELAY_HOST": "smtp.relay.example.test"})
    assert not (tmp_path / "postfix-sasl-password.cf").exists()
    assert not (tmp_path / "postfix-relaymap.cf").exists()
    assert "RELAY_USER" in res.stdout + res.stderr


def _template_tasks(playbook):
    for play in yaml.safe_load(playbook.read_text()):
        stack = list(play.get("tasks", []) or [])
        while stack:
            task = stack.pop()
            for key in ("block", "rescue", "always"):
                stack.extend(task.get(key, []) or [])
            mod = task.get("template") or task.get("ansible.builtin.template")
            if mod:
                yield task, mod


def _expand(value, item):
    value = str(value)
    for k, v in (item or {}).items():
        value = value.replace("{{ item.%s }}" % k, str(v))
    return value.replace("{{ app }}", "skmail").replace("{{ playbook_dir }}", str(SKMAIL))


def _rendered_files(playbook):
    """(src path, dest, mode) for every template the playbook renders."""
    for task, mod in _template_tasks(playbook):
        for item in task.get("loop") or [None]:
            src = pathlib.Path(_expand(mod["src"], item))
            yield src, _expand(mod["dest"], item), _expand(mod.get("mode", ""), item)


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_skmail_env_rendered_0600_root(playbook):
    found = [
        (dest, mode) for src, dest, mode in _rendered_files(playbook) if src.name == "skmail.env.j2"
    ]
    assert found, "skmail.env template task not found"
    for dest, mode in found:
        assert mode == "0600", f"{dest} rendered {mode}"
    for task, mod in _template_tasks(playbook):
        if any("skmail.env.j2" in str(i.get("src")) for i in task.get("loop") or []):
            assert mod.get("owner") == "root"


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_no_secret_bearing_template_is_world_readable(playbook):
    checked = 0
    for src, dest, mode in _rendered_files(playbook):
        assert src.exists(), f"{src} missing"
        if not SECRET_REF.search(src.read_text()):
            continue
        checked += 1
        assert mode, f"{dest} has no explicit mode"
        assert int(mode, 8) & 0o077 == 0, f"{dest} holds a secret but is rendered {mode}"
    assert checked, "expected at least one secret-bearing template (skmail.env)"


def test_playbooks_exist():
    assert len(PLAYBOOKS) == 3
