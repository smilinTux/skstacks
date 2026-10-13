"""skhub applies the vault mail settings (skhub.smtp_*) to Nextcloud.

The role rendered SMTP_* into skhub.env, but the Nextcloud image reads those
only on its first install: an existing install never got mail_* keys and
sent no mail (seen for months on a production instance). Now a post-deploy
task (tasks/mail_config.yml) feeds the settings on STDIN to
files/nextcloud-mail-config.php inside the container, which compares
them with the effective config (password by sha256), and only on a real
difference runs `occ config:import` on a 0600 temp file it then removes.

Three layers:
* the PHP applier, run with a real `php` against a fake Nextcloud root and a
  fake `occ` (config:import merges into config.php);
* the task file through REAL ansible-playbook with a fake `docker` that
  records its argv and runs the code with the stdin it was given: the
  password is never on argv nor in the play output, a rerun reports
  changed=0, smtp disabled never calls docker;
* static: every playbook validates the keys and imports the task file.

`php` is required (GitHub's ubuntu runners have it); without it these
tests skip locally and FAIL under CI, so a missing interpreter can never
turn into a silent green.
"""
import json
import os
import pathlib
import shutil
import subprocess
import textwrap

import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
APPLIER = SKHUB / "files/nextcloud-mail-config.php"
TASKS = SKHUB / "tasks/mail_config.yml"
README = SKHUB / "README.md"
ENVS = ("dev", "staging", "prod")
PASSWORD = "Example-Smtp-Password-not-real-7f3a"
WANT = {
    "mail_smtpmode": "smtp", "mail_sendmailmode": "smtp", "mail_smtphost": "smtp.example.test",
    "mail_smtpport": 587, "mail_smtpsecure": "tls", "mail_smtpauth": True, "mail_smtpauthtype": "LOGIN",
    "mail_smtpname": "hub@example.test", "mail_smtppassword": PASSWORD,
    "mail_from_address": "hub", "mail_domain": "example.test",
}
FAKE_OCC = r"""<?php
// fake occ for the skhub mail tests: only `config:import <file>`, merged
// into config/config.php like Nextcloud's own import of a "system" block.
$root = __DIR__;
if (($argv[1] ?? '') !== 'config:import') { fwrite(STDERR, "unsupported\n"); exit(3); }
if (getenv('FAKE_OCC_FAIL')) { exit(1); }
$file = $argv[2];
file_put_contents("$root/import.log", substr(sprintf('%o', fileperms($file)), -3) . " $file\n", FILE_APPEND);
$in = json_decode(file_get_contents($file), true);
$CONFIG = [];
if (is_file("$root/config/config.php")) { include "$root/config/config.php"; }
$CONFIG = array_merge($CONFIG, $in['system']);
file_put_contents("$root/config/config.php", "<?php\n\$CONFIG = " . var_export($CONFIG, true) . ";\n");
"""

needs_php = pytest.mark.skipif(shutil.which("php") is None and not os.environ.get("CI"),
                               reason="php not installed (required, and enforced, under CI)")


def php():
    exe = shutil.which("php")
    assert exe, "php is required for the skhub mail tests under CI"
    return exe


def nc_root(tmp_path, config=None, extra=None):
    root = tmp_path / "html"
    (root / "config").mkdir(parents=True)
    (root / "occ").write_text(FAKE_OCC)
    if config is not None:
        (root / "config/config.php").write_text("<?php\n$CONFIG = " + php_array(config) + ";\n")
    for name, cfg in (extra or {}).items():
        (root / "config" / name).write_text("<?php\n$CONFIG = " + php_array(cfg) + ";\n")
    return root


def php_array(d):
    return "json_decode(" + json.dumps(json.dumps(d)).replace("$", "\\$") + ", true)"


def apply(root, want=WANT, **env):
    code = APPLIER.read_text()
    e = dict(os.environ, NC_ROOT=str(root), TMPDIR=str(root.parent), **env)
    r = subprocess.run([php(), "-r", code], input=json.dumps({"system": want}), capture_output=True,
                       text=True, env=e, timeout=60)
    assert PASSWORD not in r.stdout + r.stderr, "the applier printed the password"
    return r


def effective(root):
    r = subprocess.run([php(), "-r", "$CONFIG=[]; include $argv[1]; echo json_encode($CONFIG);",
                        str(root / "config/config.php")], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


# --- the PHP applier ---------------------------------------------------------------

@needs_php
def test_existing_install_without_mail_keys_gets_them_with_types(tmp_path):
    root = nc_root(tmp_path, config={"instanceid": "x", "dbtype": "pgsql"})
    r = apply(root)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("CHANGED: ") and set(r.stdout.split(": ", 1)[1].strip().split(",")) == set(WANT)
    cfg = effective(root)
    for key, value in WANT.items():
        assert cfg[key] == value and type(cfg[key]) is type(value), key
    assert cfg["dbtype"] == "pgsql", "other keys must be kept"


@needs_php
def test_temp_file_was_0600_and_is_gone(tmp_path):
    root = nc_root(tmp_path, config={})
    apply(root)
    perms, path = (root / "import.log").read_text().split()
    assert perms == "600"
    assert not pathlib.Path(path).exists()
    assert not list(tmp_path.glob("skhub-mail-*"))


@needs_php
def test_rerun_with_matching_values_changes_nothing(tmp_path):
    root = nc_root(tmp_path, config={})
    apply(root)
    before = (root / "config/config.php").read_bytes()
    r = apply(root)
    assert r.returncode == 0 and r.stdout.strip() == "UNCHANGED"
    assert (root / "config/config.php").read_bytes() == before
    assert len((root / "import.log").read_text().splitlines()) == 1, "occ ran again on a no-op"


@needs_php
def test_password_change_alone_is_detected_by_hash(tmp_path):
    root = nc_root(tmp_path, config=dict(WANT, mail_smtppassword="old-password"))
    r = apply(root)
    assert r.stdout.strip() == "CHANGED: mail_smtppassword"
    assert effective(root)["mail_smtppassword"] == PASSWORD


@needs_php
def test_type_difference_counts(tmp_path):
    root = nc_root(tmp_path, config=dict(WANT, mail_smtpport="587"))
    assert apply(root).stdout.strip() == "CHANGED: mail_smtpport"


@needs_php
def test_values_from_an_additional_config_file_count_as_effective(tmp_path):
    """The image's smtp.config.php (fed from SMTP_* env) already provides the
    same values: nothing to write."""
    root = nc_root(tmp_path, config={"instanceid": "x"}, extra={"smtp.config.php": WANT})
    r = apply(root)
    assert r.stdout.strip() == "UNCHANGED"
    assert not (root / "import.log").exists()


@needs_php
def test_failed_import_fails_and_still_removes_the_temp_file(tmp_path):
    root = nc_root(tmp_path, config={})
    r = apply(root, FAKE_OCC_FAIL="1")
    assert r.returncode == 1 and "config:import failed" in r.stderr
    assert not list(tmp_path.glob("skhub-mail-*"))


@needs_php
def test_bad_stdin_is_refused(tmp_path):
    root = nc_root(tmp_path, config={})
    r = subprocess.run([php(), "-r", APPLIER.read_text()], input="not json", capture_output=True, text=True,
                       env=dict(os.environ, NC_ROOT=str(root)))
    assert r.returncode == 2


# --- the task file through real Ansible -----------------------------------------------

FAKE_DOCKER = """#!/bin/bash
# fake docker: record argv, then run `exec -i -u www-data <cid> php -r <code>`
# locally with the stdin it was given.
printf '%s\\n' "$*" >> "$ARGV_LOG"
[ "$1 $2 $3 $4" = "exec -i -u www-data" ] || { echo "unexpected docker call" >&2; exit 9; }
[ "$6 $7" = "php -r" ] || { echo "unexpected exec" >&2; exit 9; }
exec "$PHP" -r "$8"
"""


def run_play(tmp_path, skhub):
    root = tmp_path / "html"
    if not root.exists():
        nc_root(tmp_path, config={"instanceid": "x"})
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)
    pdir = tmp_path / "pb"
    pdir.mkdir(exist_ok=True)
    if not (pdir / "files").exists():
        (pdir / "files").symlink_to(SKHUB / "files")  # playbook_dir/files/... as in the role
    play = pdir / "play.yml"
    play.write_text(yaml.safe_dump([{
        "hosts": "localhost", "gather_facts": False,
        "vars": {"app": "skhub", "env": "dev", "skhub": skhub,
                 "nextcloud_container": {"stdout": "cid123\n"}, "nextcloud_node": {"stdout": "localhost"}},
        "environment": {"PATH": f"{bindir}:{os.environ['PATH']}", "NC_ROOT": str(root),
                        "TMPDIR": str(tmp_path), "ARGV_LOG": str(tmp_path / "argv.log"),
                        "PHP": shutil.which("php") or "/bin/false"},
        "tasks": [{"import_tasks": str(TASKS)}],
    }]))
    env = dict(os.environ, ANSIBLE_NOCOLOR="1", ANSIBLE_HOST_KEY_CHECKING="0")
    r = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", "-v", str(play)],
                       capture_output=True, text=True, env=env, timeout=300)
    return r, root


ENABLED = {"smtp_enabled": True, "smtp_host": "smtp.example.test", "smtp_port": "587", "smtp_secure": "tls",
           "smtp_name": "hub@example.test", "smtp_password": PASSWORD, "mail_from_address": "hub",
           "mail_domain": "example.test"}
needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


@needs_php
@needs_ansible
def test_play_applies_once_and_never_exposes_the_password(tmp_path):
    r, root = run_play(tmp_path, ENABLED)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert PASSWORD not in r.stdout + r.stderr, "the password reached the play output"
    argv = (tmp_path / "argv.log").read_text()
    assert PASSWORD not in argv, "the password reached docker's argv"
    assert "exec -i -u www-data cid123 php -r" in argv
    assert "changed=1" in r.stdout
    cfg = effective(root)
    assert cfg["mail_smtpport"] == 587 and cfg["mail_smtpauth"] is True and cfg["mail_smtppassword"] == PASSWORD
    assert "CHANGED: " in r.stdout and "mail_smtphost" in r.stdout

    r2, _ = run_play(tmp_path, ENABLED)
    assert r2.returncode == 0 and "changed=0" in r2.stdout and "UNCHANGED" in r2.stdout
    assert PASSWORD not in r2.stdout + r2.stderr


@needs_ansible
@pytest.mark.parametrize("skhub", [{}, {"smtp_enabled": False, "smtp_password": PASSWORD}], ids=["unset", "false"])
def test_disabled_never_calls_docker(tmp_path, skhub):
    r, root = run_play(tmp_path, skhub)
    assert r.returncode == 0, r.stdout[-3000:]
    assert not (tmp_path / "argv.log").exists()
    assert "changed=0" in r.stdout and PASSWORD not in r.stdout


# --- static -----------------------------------------------------------------------------

def test_task_is_no_log_and_the_password_only_travels_on_stdin():
    tasks = yaml.safe_load(TASKS.read_text())
    apply_task = tasks[0]
    assert apply_task["no_log"] is True
    assert "smtp_password" not in apply_task["shell"] and "smtp_password" not in json.dumps(apply_task.get("environment"))
    assert "skhub.smtp_password" in apply_task["args"]["stdin"]
    assert "smtp_enabled" in apply_task["when"]


@pytest.mark.parametrize("env_name", ENVS)
def test_every_playbook_validates_and_imports_the_mail_task(env_name):
    pb = yaml.safe_load((SKHUB / f"deploy_skhub-{env_name}.yml").read_text())
    play = next(p for p in pb if "pre_tasks" in p)
    names = [t.get("name", "") for t in play.get("pre_tasks", [])]
    assert any("mail settings" in n for n in names), names
    imports = [t for t in play["tasks"] if t.get("import_tasks") == "tasks/mail_config.yml"]
    assert len(imports) == 1


def test_readme_documents_the_mail_keys():
    text = README.read_text()
    assert "## Mail (SMTP)" in text
    for key in ("smtp_enabled", "smtp_host", "smtp_port", "smtp_secure", "smtp_authtype", "smtp_name",
                "smtp_password", "mail_from_address", "mail_domain"):
        assert f"`{key}`" in text, key
