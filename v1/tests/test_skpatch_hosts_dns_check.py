"""src/skpatch-hosts/skpatch-dns-check.sh: a host that serves DNS must not run
NetBird's DNS, and nothing but the DNS server may hold a non-loopback port 53.

Run through sh, dash and bash against a stub `ss` and fake NetBird configs
under SKP_DNS_ROOT. The live failure this encodes: NetBird bound
<netbird-ip>:53 at boot, Pi-hole could not bind 0.0.0.0:53, and every package
run on the DNS host failed until `netbird up --disable-dns`."""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1] / "ansible" / "optional" / "skpatch-hosts"
SCRIPT = ROLE / "src" / "skpatch-hosts" / "skpatch-dns-check.sh"
SHELLS = [s for s in ("sh", "dash", "bash") if shutil.which(s)]

RESOLVED = ['udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=9,fd=14))',
            'tcp LISTEN 0 4096 127.0.0.54:53 0.0.0.0:* users:(("systemd-resolve",pid=9,fd=17))']
PIHOLE = ['udp UNCONN 0 0 0.0.0.0:53 0.0.0.0:* users:(("pihole-FTL",pid=7,fd=4))',
          'tcp LISTEN 0 32 0.0.0.0:53 0.0.0.0:* users:(("pihole-FTL",pid=7,fd=5))']
NETBIRD = ['udp UNCONN 0 0 10.250.0.9:53 0.0.0.0:* users:(("netbird",pid=3,fd=44))',
           'tcp LISTEN 0 4096 10.250.0.9:53 0.0.0.0:* users:(("netbird",pid=3,fd=45))']


def run(tmp, shell, ss_lines, force=False, files=None, pihole_bin=False):
    root = tmp / "root"
    (tmp / "bin").mkdir(exist_ok=True)
    out = tmp / "ss.out"
    out.write_text("\n".join(ss_lines) + ("\n" if ss_lines else ""))
    ss = tmp / "bin" / "ss"
    ss.write_text(f'#!/bin/sh\n[ "$1" = "-Hlnptu" ] || exit 9\ncat {out}\n')
    ss.chmod(0o755)
    for rel, text in (files or {}).items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    if pihole_bin:
        b = root / "usr/bin/pihole-FTL"
        b.parent.mkdir(parents=True, exist_ok=True)
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)
    root.mkdir(exist_ok=True)
    env = {"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "SKP_DNS_ROOT": str(root)}
    proc = subprocess.run([shell, str(SCRIPT), "true" if force else "false"], env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    return lines[0], [l[len("conflict: "):] for l in lines if l.startswith("conflict: ")]


@pytest.fixture(params=SHELLS)
def shell(request):
    return request.param


NB_ON = '{"ManagementURL": {"Host": "nb.example.test"}, "DisableDNS": false}'
NB_OFF = '{"ManagementURL": {"Host": "nb.example.test"},\n "DisableDNS": true}'


def test_a_plain_host_with_only_the_resolved_stub_is_not_a_dns_host(tmp_path, shell):
    assert run(tmp_path, shell, RESOLVED + NETBIRD, files={"var/lib/netbird/default.json": NB_ON}) == ("dns-host: no", [])


def test_pihole_and_netbird_on_port_53_is_one_conflict_per_listener(tmp_path, shell):
    head, conflicts = run(tmp_path, shell, RESOLVED + PIHOLE + NETBIRD)
    assert head == "dns-host: yes"
    assert conflicts == ["netbird is bound to 10.250.0.9:53 on a DNS server (run: netbird up --disable-dns)"]


def test_netbird_won_the_port_at_boot_and_pihole_is_down(tmp_path, shell):
    # the live case: only netbird listens, Pi-hole is installed but not running
    head, conflicts = run(tmp_path, shell, RESOLVED + NETBIRD, pihole_bin=True,
                          files={"var/lib/netbird/default.json": NB_ON})
    assert head == "dns-host: yes"
    assert any(c.startswith("netbird is bound to 10.250.0.9:53") for c in conflicts)
    assert "no DNS server is listening on a non-loopback port 53" in conflicts
    assert any(c.startswith("NetBird DNS is enabled in /var/lib/netbird/default.json") for c in conflicts)


@pytest.mark.parametrize("rel", ["etc/netbird/config.json", "var/lib/netbird/default.json", "var/lib/netbird/u1/work.json"])
def test_netbird_dns_left_on_in_any_profile_is_flagged_and_disabled_is_clean(tmp_path, shell, rel):
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    _, on = run(tmp_path / "on", shell, PIHOLE, files={rel: NB_ON.replace(', "DisableDNS": false', "")})
    assert on == [f"NetBird DNS is enabled in /{rel}: at boot it can take port 53 before the DNS server "
                  "(run: netbird up --disable-dns)"]
    assert run(tmp_path / "off", shell, PIHOLE, files={rel: NB_OFF}) == ("dns-host: yes", [])


def test_backups_and_state_files_are_not_read_as_profiles(tmp_path, shell):
    files = {"var/lib/netbird/default.json.holoconnect-bak-20261108": NB_ON,
             "var/lib/netbird/state.json": '{"DisableDNS": false}'}
    assert run(tmp_path, shell, PIHOLE, files=files) == ("dns-host: yes", [])


def test_another_process_on_port_53_of_a_dns_host_is_a_conflict(tmp_path, shell):
    other = ['udp UNCONN 0 0 192.0.2.5:53 0.0.0.0:* users:(("tailscaled",pid=4,fd=9))']
    _, conflicts = run(tmp_path, shell, PIHOLE + other)
    assert conflicts == ["tailscaled holds 192.0.2.5:53 on a DNS server"]


def test_the_host_var_forces_the_dns_rules_when_nothing_is_running(tmp_path, shell):
    assert run(tmp_path, shell, RESOLVED, force=True) == (
        "dns-host: yes", ["no DNS server is listening on a non-loopback port 53"])


def test_playbook_runs_the_probe_read_only_and_only_warns():
    plays = yaml.safe_load((ROLE / "provision_skpatch_hosts_nodes.yml").read_text())
    pre = plays[0]["pre_tasks"]
    probe = next(t for t in pre if t["name"] == "Check a DNS server host for anything else on port 53 (read-only)")
    warn = next(t for t in pre if t["name"] == "Warn about a DNS port conflict")
    assert probe["check_mode"] is False and probe["changed_when"] is False and probe["failed_when"] is False
    assert "skpatch-dns-check.sh" in probe["script"] and "skpatch_hosts_dns_server" in probe["script"]
    assert "debug" in warn and "fail" not in warn
