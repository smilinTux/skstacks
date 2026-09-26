"""sksso bind mounts must match what a live Authentik needs: every /var/data
directory mounted is created by the deploy playbook, and the optional
user_settings.py override is a single file mounted on server and worker only
when the instance provides it. The old template mounted a /data directory
nobody created, so swarm rejected every server task (skstack06 v2.17.0 run)."""
import pathlib
import re

import jinja2
import yaml

SKSSO = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/sksso"


def render(**sksso):
    base = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}
    base.update(sksso)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string((SKSSO / "src/config/sksso/sksso.yml.j2").read_text()).render(env="dev", app="sksso", sksso=base)
    return yaml.safe_load(out)["services"]


def test_no_data_dir_mount_by_default():
    for name in ("server", "worker"):
        vols = [str(v) for v in render()[name].get("volumes", [])]
        assert not any(v.split(":")[1] == "/data" for v in vols if v.count(":") >= 1), (name, vols)
        assert not any(":/data/user_settings.py" in v for v in vols), (name, vols)


def test_user_settings_file_mounted_when_set():
    svcs = render(user_settings_py="TENANT_APPS = []\n")
    for name in ("server", "worker"):
        vols = [str(v) for v in svcs[name]["volumes"]]
        assert any(v.startswith("/var/data/config/sksso-dev/custom/user_settings.py:/data/user_settings.py") for v in vols), (name, vols)


def test_every_mounted_var_data_dir_is_created():
    created = set()
    pb = (SKSSO / "deploy_sksso-dev.yml").read_text()
    for m in re.finditer(r"^\s+- (/var/data/.+?)\s*$", pb, re.M):
        created.add(m.group(1).replace("{{ app }}", "sksso").replace("{{ env }}", "dev"))
    for name, svc in render(user_settings_py="x = 1\n").items():
        for v in svc.get("volumes", []):
            src = str(v).split(":")[0]
            if not src.startswith("/var/data/") or src.endswith((".py", ".env", ".yml", ".ini")):
                continue
            assert src in created, f"{name}: {src} is mounted but never created by the playbook"
