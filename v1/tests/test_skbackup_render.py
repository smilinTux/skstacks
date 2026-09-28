"""skbackup is a host-level app: its deploy renders a config file, a CLI
wrapper, systemd units, sanoid.conf and restic credentials on a storage host
instead of a swarm stack. These tests render the REAL deploy playbook through
Ansible (the render gate's rewriter, see skbackup_support.ansible_render) in
default and white-label mode and assert on what it would write."""
import re

import pytest

from skbackup_support import (WHITE_LABEL, ansible_render, deep_merge, full_vault, needs_ansible,
                              rendered, source_conf)

pytestmark = needs_ansible


@pytest.fixture(scope="module")
def default_out(tmp_path_factory):
    res, out = ansible_render(tmp_path_factory.mktemp("skb-default"), full_vault())
    assert res["status"] == "PASS", res["findings"]
    return out


@pytest.fixture(scope="module")
def wl_out(tmp_path_factory):
    vault = full_vault(branding=WHITE_LABEL, unit_prefix="acme-")
    res, out = ansible_render(tmp_path_factory.mktemp("skb-wl"), vault)
    assert res["status"] == "PASS", res["findings"]
    return out


def units(out):
    return {p.name: p.read_text() for p in (out / "etc/systemd/system").iterdir()}


def test_default_install_paths_and_config(default_out):
    files = rendered(default_out)
    for path in ("usr/local/sbin/skbackup", "etc/skbackup/skbackup.conf", "etc/skbackup/offsite.env",
                 "etc/skbackup/offsite.pass", "etc/sanoid/sanoid.conf", "_copy/usr/local/lib/skbackup/backup"):
        assert path in files, sorted(files)
    conf = source_conf(default_out / "etc/skbackup/skbackup.conf", "BRAND_PRODUCT", "BRAND_SHORT",
                       "BRAND_ALERT_PREFIX", "BRAND_CREDITS_TEXT", "SNAP_BACKEND", "DATA_TARGET",
                       "STATE_DIR", "OFFSITE_TAGS", "HOST_LABEL")
    assert conf["BRAND_PRODUCT"] == "SKBackup"
    assert conf["BRAND_SHORT"] == "skbackup"
    assert conf["BRAND_ALERT_PREFIX"] == "SKBackup"
    assert conf["BRAND_CREDITS_TEXT"] == ""
    assert conf["SNAP_BACKEND"] == "zfs" and conf["DATA_TARGET"] == "tank/data"
    assert conf["STATE_DIR"] == "/var/lib/skbackup"
    assert conf["OFFSITE_TAGS"].splitlines() == ["skbackup"]
    assert conf["HOST_LABEL"] == "localhost"  # the render gate's inventory host


def test_cli_wrapper_points_at_engine_and_config(default_out):
    text = (default_out / "usr/local/sbin/skbackup").read_text()
    assert text.startswith("#!/bin/bash")
    assert "/usr/local/lib/skbackup/backup" in text and "--conf /etc/skbackup/skbackup.conf" in text


def test_default_units_named_and_described_by_brand(default_out):
    u = units(default_out)
    assert set(u) == {f"skbackup-{t}.{k}" for t in ("run", "check", "prune", "restore-test") for k in ("service", "timer")}
    svc = u["skbackup-run.service"]
    assert "Description=SKBackup: " in svc
    assert "ExecStart=/usr/local/sbin/skbackup run" in svc
    assert "OnCalendar=*-*-* 05:30:00" in u["skbackup-run.timer"]
    assert "OnCalendar=Sun *-*-01..07 08:00:00" in u["skbackup-restore-test.timer"]


def test_white_label_names_prefix_and_descriptions(wl_out):
    files = rendered(wl_out)
    assert "usr/local/sbin/acmevault" in files and "etc/acmevault/acmevault.conf" in files
    assert "_copy/usr/local/lib/acmevault/backup" in files
    u = units(wl_out)
    assert "acme-acmevault-run.service" in u and "acme-acmevault-check.timer" in u
    assert all("Description=Acme Vault: " in t for n, t in u.items())
    assert "ExecStart=/usr/local/sbin/acmevault run" in u["acme-acmevault-run.service"]
    conf = source_conf(wl_out / "etc/acmevault/acmevault.conf", "BRAND_PRODUCT", "BRAND_TAGLINE",
                       "BRAND_ALERT_PREFIX", "BRAND_FOOTER", "BRAND_CONTACT", "BRAND_LOGO_URL",
                       "UNIT_BASE", "OFFSITE_TAGS", "SNAP_PREFIX")
    assert conf["BRAND_PRODUCT"] == "Acme Vault"
    assert conf["BRAND_TAGLINE"] == WHITE_LABEL["tagline"]
    assert conf["BRAND_ALERT_PREFIX"] == "ACME-VAULT"
    assert conf["BRAND_FOOTER"] == WHITE_LABEL["report_footer"]
    assert conf["BRAND_CONTACT"] == "support@acme.example"
    assert conf["BRAND_LOGO_URL"] == "https://acme.example/logo.png"
    assert conf["UNIT_BASE"] == "acme-acmevault"
    assert conf["OFFSITE_TAGS"].splitlines() == ["acmevault"]
    assert conf["SNAP_PREFIX"] == "acmevault"


def test_credits_line_is_optional(tmp_path):
    res, out = ansible_render(tmp_path, full_vault(branding=deep_merge(WHITE_LABEL, {"credits": True})))
    assert res["status"] == "PASS", res["findings"]
    conf = source_conf(out / "etc/acmevault/acmevault.conf", "BRAND_CREDITS_TEXT")
    assert "SKStacks" in conf["BRAND_CREDITS_TEXT"]


def test_sanoid_conf_carries_dataset_and_retention(default_out):
    text = (default_out / "etc/sanoid/sanoid.conf").read_text()
    assert "[tank/data]" in text
    for key, value in (("hourly", 36), ("daily", 30), ("weekly", 8), ("monthly", 6)):
        assert re.search(rf"^\s*{key}\s*=\s*{value}\s*$", text, re.M), key
    assert re.search(r"^\s*autosnap\s*=\s*yes", text, re.M)
    assert "managed-by: skbackup" in text


def test_builtin_engine_gets_a_snapshot_timer_and_no_sanoid(tmp_path):
    vault = full_vault(snapshot_backend="dir", data_target="/srv/data", snapshots={"engine": "builtin"},
                       copy={"target": "/srv/backup"})
    res, out = ansible_render(tmp_path, vault)
    assert res["status"] == "PASS", res["findings"]
    assert not (out / "etc/sanoid/sanoid.conf").exists()
    u = units(out)
    assert "skbackup-snapshot.timer" in u and "ExecStart=/usr/local/sbin/skbackup snapshot" in u["skbackup-snapshot.service"]


def test_disabled_tiers_render_no_units_or_secrets(tmp_path):
    vault = {"data_target": "tank/data"}
    res, out = ansible_render(tmp_path, vault)
    assert res["status"] == "PASS", res["findings"]
    assert set(units(out)) == {"skbackup-check.service", "skbackup-check.timer"}
    assert not (out / "etc/skbackup/offsite.pass").exists()
    conf = source_conf(out / "etc/skbackup/skbackup.conf", "COPY_ENABLED", "OFFSITE_ENABLED", "DUMPS_ENABLED",
                       "RESTORE_TEST_ENABLED", "SNAPSHOTS_ENABLED", "ALERTS_ENABLED")
    assert conf == {"COPY_ENABLED": "0", "OFFSITE_ENABLED": "0", "DUMPS_ENABLED": "0",
                    "RESTORE_TEST_ENABLED": "0", "SNAPSHOTS_ENABLED": "1", "ALERTS_ENABLED": "1"}


def test_restic_secrets_live_only_in_the_secret_files(default_out):
    conf = (default_out / "etc/skbackup/skbackup.conf").read_text()
    assert "example-restic-password-not-a-secret" not in conf
    assert "example-key-not-a-secret" not in conf
    assert (default_out / "etc/skbackup/offsite.pass").read_text().strip() == "example-restic-password-not-a-secret"
    env = source_conf(default_out / "etc/skbackup/offsite.env", "RESTIC_REPOSITORY", "RESTIC_PASSWORD_FILE",
                      "AWS_SECRET_ACCESS_KEY")
    assert env == {"RESTIC_REPOSITORY": "s3:https://s3.example.test/bucket/host",
                   "RESTIC_PASSWORD_FILE": "/etc/skbackup/offsite.pass",
                   "AWS_SECRET_ACCESS_KEY": "example-key-not-a-secret"}


def test_records_for_apps_sets_hooks_and_checks(default_out):
    conf = source_conf(default_out / "etc/skbackup/skbackup.conf", "COPY_APPS", "OFFSITE_SETS", "DUMP_HOOKS",
                       "DUMP_CHECKS", "OFFSITE_FORGET_ARGS", "OFFSITE_LIMIT_UPLOAD_KBPS", "COPY_BWLIMIT_KBPS")
    assert conf["COPY_APPS"].splitlines() == ["app-one|14|4|3|/cache", "app-two|7|4|3"]
    assert conf["OFFSITE_SETS"].splitlines() == ["keep|copy|app-one,app-two|nightly|", "photos|data|app-three||thumbs"]
    assert conf["DUMP_HOOKS"].splitlines() == ["app-one-db|1800|/usr/local/bin/dump-app-one --out /data/app-one/dump"]
    assert conf["DUMP_CHECKS"].splitlines() == ["app-one|dump/*.sql.gz|26"]
    assert conf["OFFSITE_FORGET_ARGS"].split() == ["--keep-daily", "14", "--keep-weekly", "8", "--keep-monthly", "12"]
    assert conf["OFFSITE_LIMIT_UPLOAD_KBPS"] == "8000" and conf["COPY_BWLIMIT_KBPS"] == "40000"


def test_values_with_shell_metacharacters_survive_quoting(tmp_path):
    brand = deep_merge(WHITE_LABEL, {"tagline": "it's $HOME `id` \"quoted\"; rm -rf /"})
    res, out = ansible_render(tmp_path, full_vault(branding=brand))
    assert res["status"] == "PASS", res["findings"]
    conf = source_conf(out / "etc/acmevault/acmevault.conf", "BRAND_TAGLINE")
    assert conf["BRAND_TAGLINE"] == brand["tagline"]


@pytest.mark.parametrize("vault,needle", [
    ({"data_target": ""}, "data_target"),
    (full_vault(offsite={"password": ""}), "offsite.password"),
    (full_vault(offsite={"repository": ""}), "offsite.repository"),
    (full_vault(branding={"short_name": "Bad Name"}), "short_name"),
    (full_vault(branding={"product_name": ""}), "branding"),
    (full_vault(snapshot_backend="lvm"), "snapshot_backend"),
    (full_vault(snapshot_backend="dir", data_target="/srv/data", copy={"target": "/srv/b"}), "sanoid"),
    (full_vault(copy={"target": ""}), "copy.target"),
    (full_vault(offsite={"sets": [{"name": "x", "source": "tape", "apps": ["a"]}]}), "source"),
    (full_vault(offsite={"sets": [{"name": "x", "source": "copy", "apps": ["not-in-copy"]}]}), "copy.apps"),
    (full_vault(alerts={"notifier": "command", "command": ""}), "alerts.command"),
    (full_vault(offsite={"install": "url", "url": "https://example.test/restic.bz2", "sha256": ""}), "sha256"),
    (full_vault(ui_password="x"), "Duplicati"),
    (full_vault(jobs=[]), "Duplicati"),
])
def test_deploy_fails_closed_on_bad_vault(tmp_path, vault, needle):
    res, _ = ansible_render(tmp_path, vault)
    assert res["status"] == "FAIL"
    assert needle in res["log"], res["log"][-3000:]


def test_sanoid_config_dir_is_created_before_the_config():
    """Ubuntu's sanoid package ships no /etc/sanoid (skstack06, 2026-09-28:
    "Destination directory /etc/sanoid does not exist")."""
    import yaml
    from skbackup_support import APP
    for env in ("dev", "staging", "prod"):
        tasks = yaml.safe_load((APP / f"deploy_skbackup-{env}.yml").read_text())[1]["tasks"]
        names = [t.get("name", "") for t in tasks]
        mk = [i for i, t in enumerate(tasks) if (t.get("file") or {}).get("path") == "/etc/sanoid"
              and t["file"].get("state") == "directory"]
        conf = names.index("Render sanoid.conf (tier 1 retention)")
        assert mk and mk[0] < conf, env
