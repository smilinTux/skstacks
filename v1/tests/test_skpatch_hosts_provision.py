"""provision_skpatch_hosts_nodes.yml rendered through REAL Ansible, same harness
machinery as render/render_playbooks.py (user/group/authorized_key/file
tasks are stubbed, never touching this test host's real accounts; the
sudoers template IS rendered for real, so its exact content is checked).
Covers: the per-host actions ceiling (vault ALLOW_ACTIONS vs a host's own
skpatch_hosts_allow_actions, the host var always wins), the missing/malformed
public key guard, present/absent, and -- because the render harness
rewrites `template:` into a plain Jinja2 set_fact (it never calls the
real `ansible.builtin.template` module, so `validate: ...` is NEVER
actually exercised there) -- a separate, real `visudo -cf` check against
the rendered content itself, for both the read-only and action-enabled
variants.

The command set asserted here (dpkg --audit; apt-get -o
DPkg::Lock::Timeout=60 {update -qq,upgrade -y,full-upgrade -y,
autoremove -y}; reboot) is confirmed against the shipped image's own
server bundle, not the upstream project's README -- see
src/skpatch-hosts/skpatch-hosts.sudoers.j2's header comment and the 2026-10-28
sudoers-mismatch fix for the live evidence. The commands used to be
`apt list --upgradable`, `apt-get -s dist-upgrade` and
`cat /var/run/reboot-required`, none of which the app actually runs;
that mismatch is why every live update check failed closed with
"sudo: a password is required" the first time this code path was ever
reached."""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "skpatch-hosts"
PB = ROLE / "provision_skpatch_hosts_nodes.yml"
PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexampleexampleexampleexampleexampleexamplekey example@render"

# The real, confirmed-live argv (sudoers-escaped: ":" and "=" inside a
# command's arguments are sudoers meta-characters and must be
# backslash-escaped, confirmed via visudo -cf itself -- the escaped form
# still matches the real, unescaped argv sudo sees at exec time).
APT_LOCK_WAIT = r"DPkg\:\:Lock\:\:Timeout\=60"
READ_ONLY_CMDS = ("dpkg --audit", f"apt-get -o {APT_LOCK_WAIT} update -qq")
ACTION_CMDS = (
    f"apt-get -o {APT_LOCK_WAIT} upgrade -y",
    f"apt-get -o {APT_LOCK_WAIT} full-upgrade -y",
    f"apt-get -o {APT_LOCK_WAIT} autoremove -y",
    "/usr/sbin/reboot",
)
# The old, wrong command set this file used to carry (README-derived
# guesses, never confirmed against the shipped image) -- asserted ABSENT
# everywhere below as a standing regression guard.
# The literal bare phrases also appear inside this file's own
# explanatory comment (quoting the old wrong commands by name, in
# backticks, for the record) -- so the regression guard below checks
# for the actual sudoers RULE form (NOPASSWD: <absolute path> ...),
# which never appears in comment prose, not the bare phrase.
STALE_CMDS = ("NOPASSWD: /usr/bin/apt list --upgradable",
              "NOPASSWD: /usr/bin/apt-get -s dist-upgrade",
              "NOPASSWD: /bin/cat /var/run/reboot-required",
              "NOPASSWD: /usr/bin/apt-get -y upgrade",
              "NOPASSWD: /usr/bin/apt-get -y dist-upgrade",
              "NOPASSWD: /usr/bin/apt-get -y autoremove")


def run(tmp: Path, vault: dict | None, extra_vars: dict | None = None, mutate=None) -> dict:
    work = tmp / "work"
    shutil.copytree(rp.ANSIBLE, work / "ansible")
    if mutate is not None:
        mutate(work / "ansible")
    for task_file in (work / "ansible").glob("**/tasks/*.yml"):
        if task_file.name != "select_vault_file.yml":
            rp.rewrite_task_file(task_file)

    run_dir = tmp / "run"
    vault_dir = run_dir / "vaults" / "optional" / "group_vars" / "prod"
    vault_dir.mkdir(parents=True)
    doc = {} if vault is None else {"skpatch_hosts": vault}
    (vault_dir / "skpatch-hosts-prod_vault.yml").write_text(yaml.safe_dump(doc))

    inv_dir = run_dir / "inventory"
    shutil.copytree(rp.FIXTURES, inv_dir)
    rp.write_inventory(inv_dir / "hosts.yml", "swarm_managers")
    # extra per-host vars (skpatch_hosts_allow_actions) merge onto localhost.
    inv = yaml.safe_load((inv_dir / "hosts.yml").read_text())
    inv["all"]["hosts"]["localhost"].update((extra_vars or {}))
    (inv_dir / "hosts.yml").write_text(yaml.safe_dump(inv))

    outdir = run_dir / "out"
    (outdir / "_shell").mkdir(parents=True)
    render_pb = work / "ansible" / "optional" / "skpatch-hosts" / "_render_provision.yml"
    rp.build_render_playbook(work / "ansible" / "optional" / "skpatch-hosts" / "provision_skpatch_hosts_nodes.yml", render_pb)

    env_vars = dict(__import__("os").environ,
                     ANSIBLE_LOCAL_TEMP=str(run_dir / ".ansible_tmp"),
                     ANSIBLE_HOST_KEY_CHECKING="False", ANSIBLE_RETRY_FILES_ENABLED="False",
                     ANSIBLE_DEPRECATION_WARNINGS="False", ANSIBLE_NOCOLOR="1",
                     ANSIBLE_GATHERING="smart")
    cmd = ["ansible-playbook", "-i", str(inv_dir / "hosts.yml"),
           "-e", f"render_outdir={outdir}",
           "-e", f"skstacks_vault_dir={run_dir / 'vaults'}",
           str(render_pb)]
    proc = subprocess.run(cmd, cwd=render_pb.parent, env=env_vars,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    res = {"rc": proc.returncode, "log": proc.stdout}
    if proc.returncode == 0:
        rp.unpack(outdir)
        bundle = outdir / "_render_files.json"
        res["files"] = {} if not bundle.exists() else None
    return res


def sudoers(tmp, vault, extra_vars=None) -> str:
    res = run(tmp, vault, extra_vars)
    assert res["rc"] == 0, res["log"][-4000:]
    return (tmp / "run/out/etc/sudoers.d/skpatch").read_text()


def fails(tmp, vault, extra_vars=None) -> str:
    res = run(tmp, vault, extra_vars)
    assert res["rc"] != 0, "the playbook rendered although it must refuse this vault"
    return res["log"]


def assert_no_stale_commands(text):
    for cmd in STALE_CMDS:
        assert cmd not in text, f"stale, never-actually-run command resurfaced: {cmd!r}"


def assert_visudo_accepts(text):
    """The real `visudo -cf -`, not the render harness's Jinja2-only
    stand-in (which never calls the ansible.builtin.template module, so
    `validate: '/usr/sbin/visudo -cf %s'` is never exercised there).
    This is what actually guards every managed fleet host from an
    sudoers file that would break `sudo` entirely if installed broken."""
    visudo = shutil.which("visudo")
    if visudo is None:
        pytest.skip("visudo not installed")
    proc = subprocess.run([visudo, "-cf", "-"], input=text, capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"visudo rejected the rendered sudoers file (rc={proc.returncode}):\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}\n--- content ---\n{text}"
    )


# --- happy path --------------------------------------------------------

def test_present_with_global_allow_actions_false_writes_read_only_rule(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": False})
    for cmd in READ_ONLY_CMDS:
        assert cmd in text
    for cmd in ACTION_CMDS:
        assert cmd not in text
    assert_no_stale_commands(text)
    assert "skpatch-ro ALL=(root) NOPASSWD:" in text


def test_present_with_global_allow_actions_true_adds_the_action_lines(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True})
    for cmd in READ_ONLY_CMDS + ACTION_CMDS:
        assert cmd in text
    assert_no_stale_commands(text)


def test_custom_provision_user_name_is_used_in_the_rule(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "PROVISION_USER": "skpatch-nam"})
    assert "skpatch-nam ALL=(root) NOPASSWD:" in text
    assert "skpatch-ro" not in text


# --- selected-package upgrades: wrappers, never an apt-get/snap wildcard ----

WRAPPERS = ("skpatch-apt-only-upgrade", "skpatch-snap-refresh")


def _rules(text):
    return [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def test_actions_enabled_sudoers_grants_the_two_wrappers_by_absolute_path(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True})
    for name in WRAPPERS:
        assert f"skpatch-ro ALL=(root) NOPASSWD: /usr/local/sbin/{name}\n" in text + "\n"


def test_read_only_sudoers_has_no_wrapper_lines(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": False})
    assert "/usr/local/sbin" not in "\n".join(_rules(text))


def test_host_ceiling_decides_the_wrapper_lines_like_the_apt_lines(tmp_path):
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    off = sudoers(tmp_path / "off", {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True},
                  extra_vars={"skpatch_hosts_allow_actions": False})
    on = sudoers(tmp_path / "on", {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": False},
                 extra_vars={"skpatch_hosts_allow_actions": True})
    assert "/usr/local/sbin/skpatch-apt-only-upgrade" not in off
    assert "/usr/local/sbin/skpatch-apt-only-upgrade" in on


@pytest.mark.parametrize("allow", [False, True])
def test_sudoers_has_no_wildcard_on_apt_get_or_snap(tmp_path, allow):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": allow})
    for rule in _rules(text):
        if "/apt-get" in rule or "/snap" in rule or "/apt " in rule:
            assert "*" not in rule, f"wildcard on an apt/snap rule: {rule!r}"
    # No rule anywhere may carry a wildcard, and the selected-package form of
    # apt-get is never granted directly: only through the wrapper.
    assert not [r for r in _rules(text) if "*" in r]
    assert not [r for r in _rules(text) if "install" in r and "apt-get" in r]
    assert not [r for r in _rules(text) if "--only-upgrade" in r]
    assert not [r for r in _rules(text) if r.rstrip().endswith("NOPASSWD: ALL")]


def test_rendered_sudoers_with_wrappers_passes_visudo(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True})
    assert "/usr/local/sbin/skpatch-snap-refresh" in text
    assert_visudo_accepts(text)


def test_wrappers_are_rendered_to_usr_local_sbin_only_where_actions_are_allowed(tmp_path):
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    sudoers(tmp_path / "on", {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True})
    sudoers(tmp_path / "off", {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": False})
    for name in WRAPPERS:
        shipped = (ROLE / "src" / "skpatch-hosts" / f"{name}.sh").read_bytes()
        assert (tmp_path / "on/run/out/_copy/usr/local/sbin" / name).read_bytes() == shipped
        assert not (tmp_path / "off/run/out/_copy/usr/local/sbin" / name).exists()


def test_provision_installs_the_wrappers_root_owned_0755_and_removes_them_when_not_allowed():
    """Static guard (the render harness never applies owner/mode): root:root
    0755, an absolute destination under /usr/local/sbin, gated exactly like
    the apt upgrade lines, removed on de-provision and when a host drops back
    to report-only."""
    pb = PB.read_text()
    inst = _task(pb, "Install the selected-package upgrade wrappers (present, actions allowed)")
    assert "owner: root" in inst and "group: root" in inst and "mode: '0755'" in inst
    assert 'dest: "/usr/local/sbin/{{ item }}"' in inst
    assert "skpatch-apt-only-upgrade" in inst and "skpatch-snap-refresh" in inst
    assert "when: skpatch_state == 'present' and skp_node_allow_actions | bool" in inst
    gone = _task(pb, "Remove the selected-package upgrade wrappers (absent, or actions no longer allowed)")
    assert "state: absent" in gone
    assert "when: skpatch_state == 'absent' or not (skp_node_allow_actions | bool)" in gone
    # installed before the sudoers rule that names them
    assert pb.index("Install the selected-package upgrade wrappers") < pb.index("Write the sudoers rule (present)")


# --- the real visudo, both variants (coordinator-requested safety check) ---

def test_rendered_sudoers_passes_visudo_read_only(tmp_path):
    assert_visudo_accepts(sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": False}))


def test_rendered_sudoers_passes_visudo_with_actions_enabled(tmp_path):
    assert_visudo_accepts(sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True}))


def test_provision_task_validates_with_the_real_visudo_before_install():
    """Static guard on the task itself: the install must fail closed on a
    broken file rather than ever writing one to /etc/sudoers.d, mode 0440
    (sudoers' own required mode), and the validate command must be an
    absolute path (never bare `visudo`, which depends on PATH resolution
    inside whatever environment the become/module boundary hands it)."""
    pb_text = PB.read_text()
    block = pb_text.split("Write the sudoers rule (present)")[1].split("- name:")[0]
    assert "validate: '/usr/sbin/visudo -cf %s'" in block
    assert "mode: '0440'" in block
    assert "dest: \"/etc/sudoers.d/skpatch\"" in block
    assert "owner: root" in block
    assert "group: root" in block


# --- the per-host ceiling always wins -----------------------------------

def test_host_level_false_overrides_global_true(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True},
                   extra_vars={"skpatch_hosts_allow_actions": False})
    for cmd in ACTION_CMDS:
        assert cmd not in text


def test_host_level_true_overrides_global_false(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": False},
                   extra_vars={"skpatch_hosts_allow_actions": True})
    for cmd in ACTION_CMDS:
        assert cmd in text


def test_unset_host_level_falls_back_to_the_global_default(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY})  # ALLOW_ACTIONS unset -> false
    assert ACTION_CMDS[0] not in text


# --- failures -------------------------------------------------------------

@pytest.mark.parametrize("vault", [
    None,
    {},
    {"SSH_PUBLIC_KEY": ""},
    {"SSH_PUBLIC_KEY": "not-a-real-key AAAA"},
    {"SSH_PUBLIC_KEY": "rsa-sha2-512 AAAA"},
], ids=["no-block", "missing", "empty", "garbage", "wrong-prefix"])
def test_missing_or_malformed_public_key_fails_loudly(tmp_path, vault):
    log = fails(tmp_path, vault)
    assert "skpatch_hosts.SSH_PUBLIC_KEY is required" in log


def test_valid_key_types_are_each_accepted(tmp_path):
    for prefix in ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256"):
        sub = tmp_path / prefix
        sub.mkdir()
        sudoers(sub, {"SSH_PUBLIC_KEY": f"{prefix} AAAAexamplekeyexamplekeyexample example@render"})


# --- static -----------------------------------------------------------------

def test_absent_state_never_requires_a_public_key():
    pb_text = PB.read_text()
    assert "when: skpatch_state == 'present'" in pb_text
    # the pubkey guard is explicitly gated to state == present
    guard = pb_text.split("Fail loudly when the SSH public key is missing")[1].split("- name:")[0]
    assert "when: skpatch_state == 'present'" in guard


def test_absent_removes_the_account_key_and_sudoers_rule():
    pb_text = PB.read_text()
    assert "state: absent" in pb_text
    assert "remove: true" in pb_text
    assert "/etc/sudoers.d/skpatch" in pb_text


def _task(pb_text, name):
    return pb_text.split(name)[1].split("- name:")[0]


def test_account_gets_a_fixed_non_system_uid_outside_the_container_range():
    # Found on the ziowk01 gate 2026-11-06: `system: true` handed skpatch-ro uid
    # 999, the uid the postgres containers run as, so userdel refused forever
    # ("currently used by process"). The account must own a fixed uid/gid far
    # above the SYS_UID range (100..999) and the first regular users.
    pb_text = PB.read_text()
    create = _task(pb_text, "Create the skpatch-hosts account (present)")
    assert "system: true" not in create
    assert "uid: \"{{ skpatch_uid }}\"" in create
    group = _task(pb_text, "Create the skpatch-hosts group (present)")
    assert "gid: \"{{ skpatch_uid }}\"" in group
    default = yaml.safe_load(pb_text)[0]["vars"]["skpatch_uid"]
    assert "skpatch_hosts.PROVISION_UID" in default
    assert int(default.split("default(")[1].split(",")[0].strip(" )")) >= 5000


def test_absent_terminates_the_accounts_sessions_then_forces_userdel():
    pb_text = PB.read_text()
    end = pb_text.index("End the account's own sessions and processes (absent)")
    remove = pb_text.index("Remove the account and its home, key included (absent)")
    assert end < remove
    sessions = _task(pb_text, "End the account's own sessions and processes (absent)")
    assert "loginctl terminate-user" in sessions
    assert "pkill -KILL -u" in sessions
    assert "when: skpatch_state == 'absent'" in sessions
    assert "force: true" in _task(pb_text, "Remove the account and its home, key included (absent)")


# --- repair, debconf, Proxmox reboot guard, conffile policy -----------------
# Each closes a dashboard action that failed closed on a real host: the Repair
# banner (`dpkg --configure -a`), debconf prompting with no terminal because
# sudo dropped DEBIAN_FRONTEND, a Proxmox reboot blocked by the app's own pvesh
# backup guard, and dpkg stopping at a conffile prompt with stdin on /dev/null.

PVESH = "/usr/bin/pvesh get /cluster/tasks --output-format json"
REPAIR = "skpatch-ro ALL=(root) NOPASSWD: /usr/bin/dpkg --configure -a"
ENV_KEEP = 'Defaults:skpatch-ro env_keep += "DEBIAN_FRONTEND"'
DPKG_CFG = "run/out/_copy/etc/dpkg/dpkg.cfg.d/90-skpatch-hosts"


def test_actions_grant_the_repair_command_and_keep_debian_frontend(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True})
    assert REPAIR in text
    assert ENV_KEEP in text
    # exactly one env_keep, for exactly one variable
    assert [r for r in _rules(text) if "env_keep" in r] == [ENV_KEEP]


def test_read_only_has_no_repair_no_env_keep_and_no_pvesh(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": False},
                   extra_vars={"skpatch_hosts_proxmox": True})
    rules = "\n".join(_rules(text))
    assert "--configure" not in rules
    assert "env_keep" not in rules
    assert "pvesh" not in rules


def test_pvesh_guard_is_granted_only_on_proxmox_with_actions(tmp_path):
    (tmp_path / "pve").mkdir()
    (tmp_path / "plain").mkdir()
    pve = sudoers(tmp_path / "pve", {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True},
                  extra_vars={"skpatch_hosts_proxmox": True})
    plain = sudoers(tmp_path / "plain", {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True})
    assert f"skpatch-ro ALL=(root) NOPASSWD: {PVESH}" in pve
    # the render host has no /usr/bin/pvesh: detection says not Proxmox
    assert "pvesh" not in "\n".join(_rules(plain))
    assert_visudo_accepts(pve)


def test_pvesh_rule_is_the_exact_read_only_argv(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True},
                   extra_vars={"skpatch_hosts_proxmox": True})
    pvesh_rules = [r for r in _rules(text) if "pvesh" in r]
    assert pvesh_rules == [f"skpatch-ro ALL=(root) NOPASSWD: {PVESH}"]
    assert " set " not in pvesh_rules[0] and " create " not in pvesh_rules[0]


def test_conffile_policy_is_written_only_where_actions_are_allowed(tmp_path):
    for sub in ("on", "off", "optout"):
        (tmp_path / sub).mkdir()
    sudoers(tmp_path / "on", {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True})
    sudoers(tmp_path / "off", {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": False})
    sudoers(tmp_path / "optout", {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True, "DPKG_KEEP_CONFFILES": False})
    body = (tmp_path / "on" / DPKG_CFG).read_text()
    assert [l for l in body.splitlines() if l and not l.startswith("#")] == ["force-confdef", "force-confold"]
    assert not (tmp_path / "off" / DPKG_CFG).exists()
    assert not (tmp_path / "optout" / DPKG_CFG).exists()


def test_conffile_policy_is_removed_when_off_and_written_before_sudoers():
    pb = PB.read_text()
    put = _task(pb, "Keep local conffiles during unattended upgrades (present, actions allowed)")
    assert "dest: /etc/dpkg/dpkg.cfg.d/90-skpatch-hosts" in put and "mode: '0644'" in put
    gone = _task(pb, "Remove the conffile policy (absent, actions off, or DPKG_KEEP_CONFFILES false)")
    assert "state: absent" in gone
    assert "not (skp_keep_conffiles | bool)" in gone and "skpatch_state == 'absent'" in gone


# --- pacman and flatpak (an Arch-family workstation) -------------------------

PACMAN_RO = "skpatch-ro ALL=(root) NOPASSWD: /usr/bin/pacman -Sy --noconfirm"
FLATPAK_RO = "skpatch-ro ALL=(root) NOPASSWD: /usr/bin/flatpak update --appstream"
PACMAN_ACT = ("skpatch-ro ALL=(root) NOPASSWD: /usr/bin/pacman -Syu --noconfirm",
              "skpatch-ro ALL=(root) NOPASSWD: /usr/local/sbin/skpatch-pacman-upgrade")
FLATPAK_ACT = ("skpatch-ro ALL=(root) NOPASSWD: /usr/bin/flatpak update -y",
               "skpatch-ro ALL=(root) NOPASSWD: /usr/bin/flatpak uninstall --unused -y",
               "skpatch-ro ALL=(root) NOPASSWD: /usr/local/sbin/skpatch-flatpak-update")
ARCH = {"skpatch_hosts_pkg_managers": ["pacman", "flatpak"]}


def test_pacman_and_flatpak_lines_only_where_the_manager_exists(tmp_path):
    (tmp_path / "arch").mkdir()
    (tmp_path / "deb").mkdir()
    arch = sudoers(tmp_path / "arch", {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True}, extra_vars=ARCH)
    deb = sudoers(tmp_path / "deb", {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True},
                  extra_vars={"skpatch_hosts_pkg_managers": []})
    for line in (PACMAN_RO, FLATPAK_RO) + PACMAN_ACT + FLATPAK_ACT:
        assert line in arch
    rules = "\n".join(_rules(deb))
    assert "pacman" not in rules and "flatpak" not in rules
    assert_visudo_accepts(arch)


def test_pacman_and_flatpak_report_only_get_just_the_refresh(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": False}, extra_vars=ARCH)
    assert PACMAN_RO in text and FLATPAK_RO in text
    for line in PACMAN_ACT + FLATPAK_ACT:
        assert line not in text


def test_pacman_autoremove_and_selected_forms_are_never_granted(tmp_path):
    text = sudoers(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True}, extra_vars=ARCH)
    rules = _rules(text)
    assert not [r for r in rules if "-Rns" in r]
    assert not [r for r in rules if re.search(r"/usr/bin/pacman -S( |$)", r)]
    assert not [r for r in rules if "*" in r]
    assert [r for r in rules if "/usr/bin/flatpak update" in r] == [FLATPAK_RO, FLATPAK_ACT[0]]


def test_arch_wrappers_are_installed_only_where_the_manager_exists(tmp_path):
    (tmp_path / "arch").mkdir()
    (tmp_path / "deb").mkdir()
    sudoers(tmp_path / "arch", {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True}, extra_vars=ARCH)
    sudoers(tmp_path / "deb", {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True},
            extra_vars={"skpatch_hosts_pkg_managers": []})
    for name in ("skpatch-pacman-upgrade", "skpatch-flatpak-update"):
        shipped = (ROLE / "src" / "skpatch-hosts" / f"{name}.sh").read_bytes()
        assert (tmp_path / "arch/run/out/_copy/usr/local/sbin" / name).read_bytes() == shipped
        assert not (tmp_path / "deb/run/out/_copy/usr/local/sbin" / name).exists()


def test_conffile_policy_needs_dpkg():
    put = _task(PB.read_text(), "Keep local conffiles during unattended upgrades (present, actions allowed)")
    assert "skp_has_dpkg | bool" in put


# --- onboarding guards: maintenance pause, uncovered package managers ------

def test_paused_host_is_left_completely_alone(tmp_path):
    res = run(tmp_path, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True},
              extra_vars={"skpatch_hosts_paused": True})
    assert res["rc"] == 0, res["log"][-4000:]
    assert "PAUSED" in res["log"]
    assert not (tmp_path / "run/out/etc/sudoers.d/skpatch").exists()
    assert not (tmp_path / "run/out/_copy/usr/local/sbin").exists()


def test_pause_marker_file_is_checked_and_the_pause_ends_the_host_before_any_change():
    text = PB.read_text()
    assert "/etc/skpatch-hosts.paused" in text
    pause = text.index("Look for the maintenance pause marker")
    assert pause < text.index("Create the skpatch-hosts account (present)")
    assert pause < text.index("Write the sudoers rule (present)")
    assert "meta: end_host" in text[pause:text.index("Select vault file")]


def test_paused_host_is_not_registered_in_the_dashboard():
    collect = _task(PB.read_text(), "Collect this run's target systems (name, hostname, port) from the play's own hosts")
    assert "hostvars[item].skp_paused" in collect


def test_unsupported_manager_is_refused_unless_acknowledged():
    text = PB.read_text()
    refuse = _task(text, "Refuse a host with an uncovered package manager")
    assert "skpatch_hosts_unsupported_managers_ok" in refuse
    assert "sudo-coverage.yml" in _task(text, "Load the sudo coverage policy (which managers this role covers)")
    assert text.index("Refuse a host with an uncovered package manager") < text.index("Write the sudoers rule (present)")


def test_unsupported_manager_detection_fails_the_render(tmp_path):
    # Point one "unsupported" binary at a path that exists on every test host
    # (in the test's own copy of the tree), so detection fires for real
    # through Ansible's stat.
    def fake_dnf(ansible):
        cov = ansible / "optional" / "skpatch-hosts" / "src" / "skpatch-hosts" / "sudo-coverage.yml"
        cov.write_text(cov.read_text().replace("dnf: /usr/bin/dnf", "dnf: /bin/sh"))

    vault = {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": True}
    (tmp_path / "bad").mkdir()
    res = run(tmp_path / "bad", vault, mutate=fake_dnf)
    assert res["rc"] != 0 and "does not cover" in res["log"] and "dnf" in res["log"]
    assert not (tmp_path / "bad/run/out/etc/sudoers.d/skpatch").exists()
    (tmp_path / "ok").mkdir()
    res = run(tmp_path / "ok", vault, {"skpatch_hosts_unsupported_managers_ok": ["dnf"]}, mutate=fake_dnf)
    assert res["rc"] == 0, res["log"][-4000:]
