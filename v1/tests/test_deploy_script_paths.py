"""A service's deploy script must look where its playbook writes. Playbooks
write /var/data/config/<app>-<env>/; a deploy.j2 that only adds the env
suffix for prod finds no compose file in dev/staging (skstack06 caught skmon)."""
import pathlib
import re

import jinja2

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"


def test_deploy_script_config_dir_matches_playbooks():
    bad = []
    for tpl in sorted(ANSIBLE.glob("*/*/src/*/deploy.j2")):
        app = tpl.parents[2].name
        src = tpl.read_text()
        if not re.search(r'^CONFIG_DIR=', src, re.M):
            continue
        for env in ("dev", "staging", "prod"):
            rendered = jinja2.Environment(undefined=jinja2.ChainableUndefined).from_string(src).render(app=app, env=env, app_networks=[])
            shell = {"APP": app}
            got = None
            for line in rendered.splitlines():  # resolve simple NAME="..." assignments in order
                a = re.match(r'^([A-Z_]+)="?([^"\s]*)"?\s*$', line)
                if a:
                    shell[a.group(1)] = re.sub(r"\$\{(\w+)\}", lambda v: shell.get(v.group(1), v.group(0)), a.group(2))
                    if a.group(1) == "CONFIG_DIR":
                        got = shell["CONFIG_DIR"]
                        break
            if got != f"/var/data/config/{app}-{env}":
                bad.append(f"{tpl.relative_to(ANSIBLE)} [{env}]: CONFIG_DIR={got}")
    assert not bad, "\n".join(bad)
