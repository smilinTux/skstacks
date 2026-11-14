"""Build-time drift guard: the skpatch-hosts sudoers template must cover every
sudo command the PINNED dashboard image runs, exactly, and nothing else.

Three inputs, one verdict:
  - fixtures/skpatch_hosts_sudo_commands.json: what the image itself says it
    runs with sudo (its own buildCommandReference(), extracted by
    ansible/optional/skpatch-hosts/tools/extract_sudo_commands.py). Keyed to
    the image reference.
  - src/skpatch-hosts/sudo-coverage.yml: how this role answers each command
    (exact rule, validated wrapper, or deliberately denied).
  - the sudoers template, rendered through real Ansible for an Arch+Debian
    Proxmox host with actions on, and again report-only.

A Renovate bump of skp_default_image fails test_fixture_is_for_the_pinned_image
until someone reruns the extractor; a new or changed command in the new image
then fails test_every_image_command_has_a_decision until sudo-coverage.yml and
the template agree with it.
"""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_skpatch_hosts_provision import PUBKEY, ROLE, _rules, assert_visudo_accepts, sudoers  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "skpatch_hosts_sudo_commands.json"
COVERAGE = ROLE / "src" / "skpatch-hosts" / "sudo-coverage.yml"
DEPLOYS = sorted(ROLE.glob("deploy_skpatch-hosts-*.yml"))
USER = "skpatch-ro"
READ_ONLY_CATEGORIES = {"check", "list_installed_packages", "detection", "system_info"}

fixture = json.loads(FIXTURE.read_text())
coverage = yaml.safe_load(COVERAGE.read_text())
SUPPORTED = coverage["supported"]


def esc(args: str) -> str:
    """sudoers argument escaping, same as the app's escapeSudoersArgs()."""
    return re.sub(r"([,:=])", r"\\\1", args.replace("\\", "\\\\"))


def rule_for(command: str) -> str:
    exe, _, args = command.partition(" ")
    path = coverage["paths"][exe]
    return f"{USER} ALL=(root) NOPASSWD: {path}" + (f" {esc(args)}" if args else "")


def image_rows(manager: str):
    return fixture["common"] if manager == "common" else fixture["managers"][manager]["rows"]


def decisions():
    for manager, entries in coverage["commands"].items():
        for entry in entries:
            yield manager, entry


@pytest.fixture(scope="module")
def renders(tmp_path_factory):
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook not installed")
    every = {"skpatch_hosts_pkg_managers": ["pacman", "flatpak"], "skpatch_hosts_proxmox": True}
    out = {}
    for name, allow, extra in (("full", True, every),
                               ("read_only", False, every),
                               ("plain", True, {"skpatch_hosts_pkg_managers": [], "skpatch_hosts_proxmox": False})):
        tmp = tmp_path_factory.mktemp(name)
        text = sudoers(tmp, {"SSH_PUBLIC_KEY": PUBKEY, "ALLOW_ACTIONS": allow}, extra_vars=extra)
        out[name] = (text, set(_rules(text)))
    return out


def test_fixture_is_for_the_pinned_image():
    pins = {}
    for deploy in DEPLOYS:
        m = re.search(r'^\s*skp_default_image:\s*"([^"]+)"', deploy.read_text(), re.M)
        assert m, f"{deploy.name} has no skp_default_image"
        pins[deploy.name] = m.group(1)
    assert DEPLOYS and set(pins.values()) == {fixture["image"]}, (
        f"pinned images {pins} differ from the sudo-command fixture's {fixture['image']!r}. "
        "Rerun ansible/optional/skpatch-hosts/tools/extract_sudo_commands.py, review the fixture "
        "diff and update src/skpatch-hosts/sudo-coverage.yml and the sudoers template to match.")


def test_every_image_command_has_a_decision():
    missing = []
    for manager in SUPPORTED + ["common"]:
        decided = {e["command"] for e in coverage["commands"].get(manager, [])}
        missing += [f"{manager}: {r['command']} ({r['category']})" for r in image_rows(manager)
                    if r["command"] not in decided]
    assert not missing, "the pinned image runs sudo commands sudo-coverage.yml has no decision for:\n" + "\n".join(missing)


def test_no_decision_is_for_a_command_the_image_no_longer_runs():
    stale = [f"{m}: {e['command']}" for m, e in decisions()
             if e["command"] not in {r["command"] for r in image_rows(m)}]
    assert not stale, "sudo-coverage.yml decides commands the pinned image does not run:\n" + "\n".join(stale)


def test_supported_managers_are_exactly_the_ones_with_decisions():
    assert set(coverage["commands"]) - {"common"} == set(SUPPORTED)
    assert set(SUPPORTED) <= set(fixture["managers"]), "a supported manager the image does not know"


def test_the_image_emits_no_sudo_command_it_could_not_express_for_a_supported_manager():
    bad = [w for w in fixture["warnings"] if w["pkgManager"] in SUPPORTED + [None]]
    assert not bad


def test_supported_managers_need_no_extra_env_keep():
    # deb-get needs DEBGET_TOKEN kept; none of the supported ones may need
    # anything the template does not keep (it keeps DEBIAN_FRONTEND only).
    for manager in SUPPORTED:
        assert fixture["managers"][manager]["envKeep"] == [], manager


def test_read_only_categories_are_granted_without_actions():
    for manager in SUPPORTED + ["common"]:
        cats = {r["command"]: r["category"] for r in image_rows(manager)}
        for entry in coverage["commands"].get(manager, []):
            if cats.get(entry["command"]) in READ_ONLY_CATEGORIES and entry["rule"] != "denied":
                assert entry["when"] == "read_only", f"{manager}: {entry['command']} is a check-time command"


def test_exact_decisions_are_granted_verbatim(renders):
    full, ro, plain = (renders[k][1] for k in ("full", "read_only", "plain"))
    for _manager, e in decisions():
        if e["rule"] != "exact":
            continue
        line = rule_for(e["command"])
        assert line in full, f"missing exact rule: {line}"
        if e["when"] == "read_only":
            assert line in ro, f"read-only command missing on a report-only host: {line}"
        else:
            assert line not in ro, f"action command granted on a report-only host: {line}"
        if e.get("proxmox"):
            assert line not in plain, f"Proxmox-only rule on a non-Proxmox host: {line}"


def test_wrapper_decisions_grant_the_wrapper_never_the_placeholder_form(renders):
    full = renders["full"][1]
    for _manager, e in decisions():
        if e["rule"] != "wrapper":
            continue
        assert f"{USER} ALL=(root) NOPASSWD: /usr/local/sbin/{e['wrapper']}" in full
        assert (ROLE / "src" / "skpatch-hosts" / f"{e['wrapper']}.sh").is_file()
        prefix = rule_for(e["command"].split(" <")[0])
        assert not [r for r in full if r.startswith(prefix + " ")], f"placeholder form granted: {prefix} ..."


def test_denied_decisions_are_not_granted_and_say_why(renders):
    full = renders["full"][1]
    for _manager, e in decisions():
        if e["rule"] != "denied":
            continue
        assert e.get("why"), f"denied without a reason: {e['command']}"
        assert rule_for(e["command"]) not in full


def test_every_granted_rule_is_backed_by_a_decision(renders):
    allowed = {rule_for(e["command"]) for _m, e in decisions() if e["rule"] == "exact"}
    allowed |= {f"{USER} ALL=(root) NOPASSWD: /usr/local/sbin/{e['wrapper']}"
                for _m, e in decisions() if e["rule"] == "wrapper"}
    for name in ("full", "read_only", "plain"):
        grants = {r for r in renders[name][1] if " NOPASSWD: " in r}
        assert grants <= allowed, f"{name}: rules no decision accounts for: {sorted(grants - allowed)}"
        assert not [r for r in grants if "*" in r]
    assert_visudo_accepts(renders["full"][0])


def test_every_manager_the_image_knows_is_supported_or_refused():
    known = set(fixture["managers"])
    listed = set(SUPPORTED) | set(coverage["unsupported"])
    assert known == listed, (
        f"the pinned image knows managers {sorted(known - listed)} that sudo-coverage.yml neither "
        f"supports nor lists as unsupported; stale entries: {sorted(listed - known)}")
    assert not set(SUPPORTED) & set(coverage["unsupported"])
    for path in coverage["unsupported"].values():
        assert path.startswith("/")
