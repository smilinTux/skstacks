"""skhealth's deploy rendered through REAL Ansible (the render gate's
rewriter, render/render_playbooks.py) with a given vault: what lands on a
node, what an unset optional block leaves out, and what the asserts refuse."""
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from skhealth_support import ENGINE

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

FAKE_KEY = "example-ssh-private-key-line-1\nexample-ssh-private-key-line-2\n"


def render(tmp: Path, vault: dict, env: str = "prod") -> tuple[dict, Path]:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    (vars_dir / "skhealth.example.yml").write_text(yaml.safe_dump({"skhealth": vault}))
    run_dir = work / f"skhealth-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved = rp.VARS
    rp.VARS = vars_dir
    try:
        pb = work / "ansible" / "optional" / "skhealth" / f"deploy_skhealth-{env}.yml"
        res = rp.run_one(work, "optional", "skhealth", env, pb, "base")
    finally:
        rp.VARS = saved
    res["log"] = (run_dir / "ansible.log").read_text() if (run_dir / "ansible.log").exists() else ""
    return res, run_dir / "out"


def files(out: Path) -> set[str]:
    return {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()}


def env_values(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = shlex.split(v)[0] if v else ""
    return out


FULL = {
    "cluster_label": "site-a",
    "confirm_runs": 3,
    "alerts": {"telegram_bot_token": "123:example-not-a-secret", "telegram_chat_id": "-100",
               "webhook_url": "https://hooks.example.test/skhealth?k=example"},
    "swarm": {"ignore_services": ["^skmon-"]},
    "host": {"fs_paths": ["/", "/var/lib/docker"], "fs_max_pct": 80},
    "remote": {"hosts": [{"name": "nas", "host": "192.0.2.10", "port": 2222, "zpool": True,
                          "smart_devices": ["/dev/nvme0n1", "/dev/sda"], "pvesm": True}],
               "ssh_private_key": FAKE_KEY,
               "known_hosts": ["[192.0.2.10]:2222 ssh-ed25519 AAAAexample"]},
    "tls": {"hosts": ["www.example.test", "mail.example.test:465"], "warn_days": 14},
}


@pytest.fixture(scope="module")
def minimal(tmp_path_factory):
    res, out = render(tmp_path_factory.mktemp("skh-min"), {})
    assert res["status"] == "PASS", (res["findings"], res["log"][-3000:])
    return out


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    res, out = render(tmp_path_factory.mktemp("skh-full"), FULL)
    assert res["status"] == "PASS", (res["findings"], res["log"][-3000:])
    return out


def test_default_deploy_installs_engine_config_units_and_nothing_optional(minimal):
    got = files(minimal)
    assert {"etc/skhealth/skhealth.json", "etc/skhealth/secrets.env", "etc/systemd/system/skhealth.service",
            "etc/systemd/system/skhealth.timer", "_copy/usr/local/sbin/skhealth"} <= got, sorted(got)
    assert "etc/skhealth/remote_ed25519" not in got and "etc/skhealth/known_hosts" not in got
    assert (minimal / "_copy/usr/local/sbin/skhealth").read_bytes() == ENGINE.read_bytes()


def test_unset_alert_blocks_render_no_credential_lines(minimal):
    assert env_values(minimal / "etc/skhealth/secrets.env") == {}


def test_default_runtime_config(minimal):
    cfg = json.loads((minimal / "etc/skhealth/skhealth.json").read_text())
    assert cfg["cluster"] == "render"           # the inventory's cluster_name
    assert cfg["primary"] == "auto" and cfg["confirm_runs"] == 2 and cfg["realert_hours"] == 6.0
    assert cfg["state_dir"] == "/var/lib/skhealth" and cfg["secrets_file"] == "/etc/skhealth/secrets.env"
    assert cfg["remote"]["hosts"] == [] and cfg["checks"]["tls"]["hosts"] == []
    assert cfg["checks"]["disks"] == {"enabled": True, "nvme_used_max_pct": 90}
    assert cfg["checks"]["host"]["fs_paths"] == ["/"] and cfg["checks"]["host"]["nfs_timeout_seconds"] == 5
    for secret_ish in ("token", "chat_id", "webhook", "ssh_private_key"):
        assert secret_ish not in json.dumps(cfg), f"{secret_ish} must live in secrets.env, not the config"


def test_units_use_absolute_paths_and_a_resource_cap(minimal):
    svc = (minimal / "etc/systemd/system/skhealth.service").read_text()
    assert "ExecStart=/usr/local/sbin/skhealth run" in svc
    assert "ConditionPathExists=/etc/skhealth/skhealth.json" in svc
    assert "MemoryMax=128M" in svc and "CPUQuota=25%" in svc and "Type=oneshot" in svc
    timer = (minimal / "etc/systemd/system/skhealth.timer").read_text()
    assert "OnCalendar=*:0/5" in timer and "WantedBy=timers.target" in timer


def test_full_vault_renders_credentials_key_and_known_hosts(full):
    assert env_values(full / "etc/skhealth/secrets.env") == {
        "SKHEALTH_TELEGRAM_TOKEN": "123:example-not-a-secret",
        "SKHEALTH_TELEGRAM_CHAT_ID": "-100",
        "SKHEALTH_WEBHOOK_URL": "https://hooks.example.test/skhealth?k=example",
    }
    key = (full / "_copy/etc/skhealth/remote_ed25519").read_text()
    assert key == FAKE_KEY and key.endswith("\n")
    assert (full / "etc/skhealth/known_hosts").read_text().strip() == "[192.0.2.10]:2222 ssh-ed25519 AAAAexample"
    cfg = json.loads((full / "etc/skhealth/skhealth.json").read_text())
    assert cfg["cluster"] == "site-a" and cfg["confirm_runs"] == 3
    assert cfg["remote"]["strict_host_keys"] is True
    assert cfg["remote"]["hosts"][0]["smart_devices"] == ["/dev/nvme0n1", "/dev/sda"]
    assert cfg["checks"]["host"]["fs_paths"] == ["/", "/var/lib/docker"]
    assert cfg["checks"]["host"]["fs_max_pct"] == 80 and cfg["checks"]["host"]["oom"] is True  # merged, not replaced
    assert cfg["checks"]["tls"] == {"hosts": ["www.example.test", "mail.example.test:465"], "warn_days": 14}


def test_the_engine_reads_the_rendered_files(full, tmp_path):
    """The deploy's config and secrets file are what the engine parses."""
    cfg = json.loads((full / "etc/skhealth/skhealth.json").read_text())
    secrets = tmp_path / "secrets.env"
    secrets.write_text((full / "etc/skhealth/secrets.env").read_text())
    cfg.update(secrets_file=str(secrets), state_dir=str(tmp_path / "state"), host_label="n1",
               checks={"swarm": {"enabled": False}, "nodes": {"enabled": False}, "host": {"enabled": False},
                       "disks": {"enabled": False}, "raid": {"enabled": False}, "tls": {"hosts": []}},
               remote={"hosts": []})
    (tmp_path / "c.json").write_text(json.dumps(cfg))
    r = subprocess.run([sys.executable, str(ENGINE), "--config", str(tmp_path / "c.json"), "status"],
                       capture_output=True, text=True, check=True)
    st = json.loads(r.stdout)
    assert st["telegram"] is True and st["webhook"] is True and st["cluster"] == "site-a"
    assert "example-not-a-secret" not in r.stdout


@pytest.mark.parametrize("vault,needle", [
    ({"alerts": {"telegram_bot_token": "123:x"}}, "telegram_bot_token AND telegram_chat_id"),
    ({"alerts": {"webhook_url": "ftp://x"}}, "telegram_bot_token AND telegram_chat_id"),
    ({"remote": {"hosts": [{"name": "nas", "host": "192.0.2.1", "zpool": True}]}}, "ssh_private_key"),
    ({"remote": {"hosts": [{"name": "nas", "host": "192.0.2.1", "smart_devices": ["/dev/sda; id"]}],
                 "ssh_private_key": "k"}}, "smart_devices"),
    ({"remote": {"hosts": [{"name": "nas", "host": "192.0.2.1"}], "ssh_private_key": "k"}}, "at least one of"),
    ({"disks": {"devices": ["/dev/sda; id"]}}, "disks.devices"),
    ({"disks": {"devices": ["sda"]}}, "disks.devices"),
    ({"disks": {"devices": "/dev/sda"}}, "disks.devices"),
    ({"alerts": {"webhook_hmac_secret": "s"}}, "webhook_hmac_secret needs webhook_url"),
    ({"confirm_runs": 0}, "confirm_runs"),
    ({"primary": "bad host"}, "primary"),
])
def test_invalid_vaults_stop_the_play(tmp_path, vault, needle):
    res, _ = render(tmp_path, vault)
    assert res["status"] == "FAIL"
    assert needle in res["log"], res["log"][-2000:]


def test_disabled_installs_nothing(tmp_path):
    res, out = render(tmp_path, {"enabled": False})
    assert res["status"] in ("PASS", "FAIL")
    got = files(out) if out.exists() else set()
    assert not [f for f in got if f.startswith(("etc/", "_copy/"))], sorted(got)


def test_local_smart_device_list_reaches_the_runtime_config(tmp_path):
    devs = ["/dev/disk/by-id/nvme-Example_SSD_1TB_S0000000", "/dev/disk/by-id/nvme-eui.0000000000000001"]
    res, out = render(tmp_path, {"disks": {"devices": devs}})
    assert res["status"] == "PASS", (res["findings"], res["log"][-3000:])
    cfg = json.loads((out / "etc/skhealth/skhealth.json").read_text())
    assert cfg["checks"]["disks"] == {"devices": devs, "enabled": True, "nvme_used_max_pct": 90}


def test_unset_or_empty_device_list_leaves_the_config_unchanged(minimal, tmp_path):
    res, out = render(tmp_path, {"disks": {"devices": []}})
    assert res["status"] == "PASS", (res["findings"], res["log"][-3000:])
    assert (out / "etc/skhealth/skhealth.json").read_bytes() == (minimal / "etc/skhealth/skhealth.json").read_bytes()


def test_webhook_hmac_secret_lands_only_in_secrets_env(tmp_path):
    vault = {"alerts": {"webhook_url": "https://hooks.example.test/h", "webhook_hmac_secret": "hmac-example-not-a-secret"}}
    res, out = render(tmp_path, vault)
    assert res["status"] == "PASS", (res["findings"], res["log"][-3000:])
    assert env_values(out / "etc/skhealth/secrets.env") == {
        "SKHEALTH_WEBHOOK_URL": "https://hooks.example.test/h",
        "SKHEALTH_WEBHOOK_HMAC_SECRET": "hmac-example-not-a-secret",
    }
    assert "hmac-example-not-a-secret" not in (out / "etc/skhealth/skhealth.json").read_text()


def test_full_vault_without_hmac_renders_no_hmac_line(full):
    assert "SKHEALTH_WEBHOOK_HMAC_SECRET" not in (full / "etc/skhealth/secrets.env").read_text()
