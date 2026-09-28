"""skbackup is a host-level app: its deploy renders config files, a CLI
wrapper, systemd units, sanoid.conf and restic credentials on a storage host
instead of a swarm stack. These tests render the REAL deploy playbook through
Ansible (the render gate's rewriter, see skbackup_support.ansible_render) in
default and white-label mode and assert on what it would write, including
that the engine parses the files the deploy renders for it."""
import re
import subprocess

import pytest
import yaml

from skbackup_support import (APP, LIB, WHITE_LABEL, ansible_render, deep_merge, full_vault, needs_ansible,
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


def lines(path):
    return [l for l in path.read_text().splitlines() if l and not l.startswith("#")]


def test_default_install_paths_and_config(default_out):
    files = rendered(default_out)
    for path in ("usr/local/sbin/skbackup", "etc/skbackup/skbackup.conf", "etc/skbackup/apps.conf",
                 "etc/skbackup/hooks.conf", "etc/skbackup/restic-sets.conf", "etc/skbackup/offsite.env",
                 "etc/sanoid/sanoid.conf", *(f"_copy/usr/local/lib/skbackup/{f}" for f in
                                             ("backup", "lib.sh", "sync", "restic", "check", "predeploy",
                                              "alert-host-check"))):
        assert path in files, sorted(files)
    conf = source_conf(default_out / "etc/skbackup/skbackup.conf", "BRAND_PRODUCT", "BRAND_SHORT",
                       "BRAND_ALERT_PREFIX", "BRAND_CREDITS_TEXT", "SRC_DATASET", "BAK_ROOT", "RSYNC_BWLIMIT",
                       "STATE_DIR", "RESTIC_SRC_MNT", "RESTIC_ENV_FILE", "RESTIC_SETS_FILE", "NOTIFY_MODE",
                       "RESTORE_TEST_APP", "RESTORE_TEST_SAMPLE_TAG", "APPS_FILE", "HOOKS_FILE")
    assert conf["BRAND_PRODUCT"] == "SKBackup" and conf["BRAND_SHORT"] == "skbackup"
    assert conf["BRAND_ALERT_PREFIX"] == "SKBackup" and conf["BRAND_CREDITS_TEXT"] == ""
    assert conf["SRC_DATASET"] == "tank/data" and conf["BAK_ROOT"] == "backup/copies"
    assert conf["RSYNC_BWLIMIT"] == "10000", "the conservative default (USB SSD NFS lesson)"
    assert conf["STATE_DIR"] == "/var/lib/skbackup"
    assert conf["RESTIC_SRC_MNT"] == "/run/skbackup/src", "restic needs a stable snapshot path"
    assert conf["RESTIC_ENV_FILE"] == "/etc/skbackup/offsite.env"
    assert conf["RESTIC_SETS_FILE"] == "/etc/skbackup/restic-sets.conf"
    assert conf["APPS_FILE"] == "/etc/skbackup/apps.conf" and conf["HOOKS_FILE"] == "/etc/skbackup/hooks.conf"
    assert conf["NOTIFY_MODE"] == "log"
    assert conf["RESTORE_TEST_APP"] == "app-one" and conf["RESTORE_TEST_SAMPLE_TAG"] == "photos"


def test_cli_wrapper_points_at_engine_and_config(default_out):
    text = (default_out / "usr/local/sbin/skbackup").read_text()
    assert text.startswith("#!/bin/bash")
    assert "/usr/local/lib/skbackup/backup" in text and "--conf /etc/skbackup/skbackup.conf" in text


def test_records_the_engine_reads(default_out):
    assert lines(default_out / "etc/skbackup/apps.conf") == [
        "app-one app-one 14 4 3 --exclude=/cache/ newest=database-dump:2 dump=database-dump",
        "app-two runtime/app-two 7 4 3",
    ]
    assert lines(default_out / "etc/skbackup/restic-sets.conf") == [
        "kept-apps apps",
        "photos paths app-three/originals,app-three/raw --exclude=/app-three/originals/tmp",
    ]
    assert lines(default_out / "etc/skbackup/hooks.conf") == [
        "app-one-db 1800 /usr/local/bin/dump-app-one --out /tank/data/app-one/database-dump"]


def test_the_engine_parses_the_rendered_apps_file(default_out):
    script = (f'. "{LIB}"; n=0; while IFS= read -r l; do skb_parse_app_line "$l" >/dev/null; rc=$?; '
              f'[ $rc -eq 2 ] && {{ echo "BAD: $l"; exit 1; }}; [ $rc -eq 0 ] && n=$((n+1)); '
              f'done < "{default_out}/etc/skbackup/apps.conf"; echo $n')
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.strip() == "2", proc.stdout


def test_default_units_named_described_and_ordered(default_out):
    u = units(default_out)
    assert set(u) == {f"skbackup-{t}.{k}" for t in ("sync", "offsite", "prune", "restore-test", "check")
                      for k in ("service", "timer")}
    assert all("Description=SKBackup: " in t for t in u.values())
    assert "ExecStart=/usr/local/sbin/skbackup sync" in u["skbackup-sync.service"]
    assert "TimeoutStartSec=6h" in u["skbackup-sync.service"]
    off = u["skbackup-offsite.service"]
    assert "ExecStart=/usr/local/sbin/skbackup restic backup" in off
    assert "skbackup-sync.service" in off.split("After=")[1].splitlines()[0]
    assert "RuntimeMaxSec=22h" in off and "Wants=network-online.target" in off
    assert "ExecStart=/usr/local/sbin/skbackup check --notify" in u["skbackup-check.service"]
    assert "ExecStart=/usr/local/sbin/skbackup restic forget-prune" in u["skbackup-prune.service"]
    for name, cal in (("sync", "*-*-* 05:30:00"), ("offsite", "*-*-* 06:30:00"), ("prune", "Sun *-*-* 13:00:00"),
                      ("restore-test", "*-*-01 14:00:00")):
        assert f"OnCalendar={cal}" in u[f"skbackup-{name}.timer"], name
    for t in u.values():
        assert "ConditionPathExists=/etc/skbackup/skbackup.conf" in t or "[Timer]" in t


def test_white_label_names_prefix_and_descriptions(wl_out):
    files = rendered(wl_out)
    assert "usr/local/sbin/acmevault" in files and "etc/acmevault/acmevault.conf" in files
    assert "_copy/usr/local/lib/acmevault/backup" in files
    u = units(wl_out)
    assert "acme-acmevault-sync.service" in u and "acme-acmevault-check.timer" in u
    assert all("Description=Acme Vault: " in t for t in u.values())
    assert "acme-acmevault-sync.service" in u["acme-acmevault-offsite.service"]
    conf = source_conf(wl_out / "etc/acmevault/acmevault.conf", "BRAND_PRODUCT", "BRAND_TAGLINE",
                       "BRAND_ALERT_PREFIX", "BRAND_FOOTER", "BRAND_CONTACT", "BRAND_LOGO_URL",
                       "UNIT_BASE", "SNAP_PREFIX_SYNC", "RESTIC_SRC_MNT", "STATE_DIR")
    assert conf["BRAND_PRODUCT"] == "Acme Vault"
    assert conf["BRAND_TAGLINE"] == WHITE_LABEL["tagline"]
    assert conf["BRAND_ALERT_PREFIX"] == "ACME-VAULT"
    assert conf["BRAND_FOOTER"] == WHITE_LABEL["report_footer"]
    assert conf["BRAND_CONTACT"] == "support@acme.example"
    assert conf["BRAND_LOGO_URL"] == "https://acme.example/logo.png"
    assert conf["UNIT_BASE"] == "acme-acmevault"
    assert conf["SNAP_PREFIX_SYNC"] == "acmevault"
    assert conf["RESTIC_SRC_MNT"] == "/run/acmevault/src" and conf["STATE_DIR"] == "/var/lib/acmevault"


def test_host_label_becomes_the_restic_host(tmp_path):
    res, out = ansible_render(tmp_path, full_vault(host_label="storage-7"))
    assert res["status"] == "PASS", res["findings"]
    assert source_conf(out / "etc/skbackup/skbackup.conf", "RESTIC_HOST")["RESTIC_HOST"] == "storage-7"


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


def test_sanoid_config_is_written_before_the_package_installs():
    """Ubuntu's sanoid ships no /etc/sanoid (skstack06, 2026-09-28), and the
    package starts its timer on install: directory and config come first."""
    for env in ("dev", "staging", "prod"):
        tasks = yaml.safe_load((APP / f"deploy_skbackup-{env}.yml").read_text())[1]["tasks"]
        names = [t.get("name", "") for t in tasks]
        mk = [i for i, t in enumerate(tasks) if (t.get("file") or {}).get("path") == "/etc/sanoid"]
        conf = names.index("Render sanoid.conf (tier 1 retention)")
        pkgs = names.index("Install the packages the enabled tiers need")
        assert mk and mk[0] < conf < pkgs, env


def test_disabled_tiers_render_no_units_or_secrets(tmp_path):
    res, out = ansible_render(tmp_path, {"data_dataset": "tank/data"})
    assert res["status"] == "PASS", res["findings"]
    assert set(units(out)) == {"skbackup-check.service", "skbackup-check.timer"}
    assert not (out / "etc/skbackup/offsite.env").exists()
    conf = source_conf(out / "etc/skbackup/skbackup.conf", "BAK_ROOT", "RESTIC_SETS_FILE", "RESTORE_TEST_APP")
    assert conf == {"BAK_ROOT": "", "RESTIC_SETS_FILE": "", "RESTORE_TEST_APP": ""}


def test_no_check_timer_when_notifier_is_none(tmp_path):
    res, out = ansible_render(tmp_path, {"data_dataset": "tank/data", "alerts": {"notifier": "none"}})
    assert res["status"] == "PASS", res["findings"]
    assert not (out / "etc/systemd/system").exists() or not units(out)


def test_restic_secrets_live_only_in_the_env_file(default_out):
    conf = (default_out / "etc/skbackup/skbackup.conf").read_text()
    assert "example-restic-password-not-a-secret" not in conf and "example-key-not-a-secret" not in conf
    env = source_conf(default_out / "etc/skbackup/offsite.env", "RESTIC_REPOSITORY", "RESTIC_PASSWORD",
                      "B2_ACCOUNT_ID", "B2_ACCOUNT_KEY")
    assert env == {"RESTIC_REPOSITORY": "s3:https://s3.example.test/bucket",
                   "RESTIC_PASSWORD": "example-restic-password-not-a-secret",
                   "B2_ACCOUNT_ID": "example-key-id", "B2_ACCOUNT_KEY": "example-key-not-a-secret"}


def test_values_with_shell_metacharacters_survive_quoting(tmp_path):
    brand = deep_merge(WHITE_LABEL, {"tagline": "it's $HOME `id` \"quoted\"; rm -rf /"})
    res, out = ansible_render(tmp_path, full_vault(branding=brand))
    assert res["status"] == "PASS", res["findings"]
    conf = source_conf(out / "etc/acmevault/acmevault.conf", "BRAND_TAGLINE")
    assert conf["BRAND_TAGLINE"] == brand["tagline"]


@pytest.mark.parametrize("vault,needle", [
    ({"data_dataset": ""}, "data_dataset"),
    ({"data_dataset": "/tank/data"}, "data_dataset"),
    (full_vault(offsite={"password": ""}), "offsite.password"),
    (full_vault(offsite={"repository": ""}), "offsite.repository"),
    (full_vault(branding={"short_name": "Bad Name"}), "short_name"),
    (full_vault(branding={"product_name": ""}), "branding"),
    (full_vault(copy={"target": ""}), "copy.target"),
    (full_vault(copy={"apps": [{"name": "a", "excludes": ["with space"]}]}), "whitespace"),
    (full_vault(copy={"apps": [{"name": "a", "src": "../etc"}]}), "copy.apps"),
    (full_vault(offsite={"sets": [{"name": "x", "kind": "tape"}]}), "kind"),
    (full_vault(copy={"enabled": False}, restore_test={"enabled": False}), "kind apps needs the copy tier"),
    (full_vault(offsite={"sets": [{"name": "x", "kind": "paths"}]}, restore_test={"enabled": False}), "kind paths needs paths"),
    (full_vault(restore_test={"app": "not-copied"}), "restore_test"),
    (full_vault(dumps={"hooks": [{"name": "x", "command": ""}]}), "dumps.hooks"),
    (full_vault(alerts={"notifier": "command", "command": ""}), "alerts.command"),
    (full_vault(offsite={"install": "url", "url": "https://example.test/restic.bz2", "sha256": ""}), "sha256"),
    (full_vault(ui_password="x"), "Duplicati"),
    (full_vault(jobs=[]), "Duplicati"),
])
def test_deploy_fails_closed_on_bad_vault(tmp_path, vault, needle):
    res, _ = ansible_render(tmp_path, vault)
    assert res["status"] == "FAIL"
    assert needle in res["log"], res["log"][-3000:]
