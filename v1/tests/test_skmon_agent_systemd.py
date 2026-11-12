"""skmon v2 systemd install mode and light mode for storage and hypervisor hosts (spec 7.2, 13, 18 Q2)."""
import subprocess

import yaml

from skmon_support import (AGENT_PLAY, CFG, SMART_SCRIPT, cfg, play_tasks, render, task_named, unit)

SCRAPE = CFG / "vm-scrape.yml.j2"
PINS = yaml.safe_load((CFG / "pins.yml").read_text())
A, B = "node-1.render.example.test", "node-2.render.example.test"
CTX_WITH_LIGHT_HOST = {"skmon_agent_targets": [
    {"node": A, "addr": "192.0.2.10", "role": "swarm", "light": False},
    {"node": B, "addr": "192.0.2.11", "role": "storage", "light": True},
]}


def jobs(extra_ctx):
    return {j["job_name"]: j for j in yaml.safe_load(render(SCRAPE, extra_ctx=extra_ctx))["scrape_configs"]}


def tasks(path):
    return play_tasks(path)


def exec_start(text):
    return next(l for l in text.splitlines() if l.startswith("ExecStart=")).split()


def test_unit_caps_light_mode():
    u = unit(agent_light=True)
    assert "MemoryMax=256M" in u and "CPUQuota=5%" in u


def test_unit_caps_full_mode_match_spec_13():
    u = unit(agent_light=False)
    assert "MemoryMax=384M" in u and "CPUQuota=50%" in u


def test_unit_exec_start_and_hardening():
    argv = exec_start(unit())
    assert argv[0] == "ExecStart=/usr/local/bin/skmon-alloy" and argv[1:3] == ["run", "/etc/skmon-prod/alloy/agent.alloy"]
    assert "--server.http.listen-addr=192.0.2.10:12345" in argv and "--storage.path=/var/lib/skmon-prod/alloy" in argv
    u = unit()
    assert "Restart=always" in u and "ProtectSystem=strict" in u and "ReadWritePaths=/var/lib/skmon-prod/alloy" in u


def test_experimental_flag_only_where_the_config_uses_the_wal():
    # the loki.write WAL block is the experimental feature; a light config has no loki.write
    assert "--stability.level=experimental" in exec_start(unit(agent_light=False))
    assert "wal {" in cfg(agent_mode="systemd", agent_light=False)
    assert "--stability.level=experimental" not in " ".join(exec_start(unit(agent_light=True)))
    assert "loki.write" not in cfg(agent_mode="systemd", agent_light=True)


def test_light_mode_has_no_logs_cadvisor_or_profiling():
    c = cfg(agent_mode="systemd", agent_light=True)
    assert "loki.source" not in c and "cadvisor" not in c and "pyroscope" not in c
    assert "loki.write" not in c and "blackbox" not in cfg(agent_mode="systemd", agent_light=True, agent_is_data_node=True)


def test_light_mode_collector_list():
    c = cfg(agent_mode="systemd", agent_light=True)
    line = next(l for l in c.splitlines() if l.strip().startswith("set_collectors ="))
    got = set(line.split("[")[1].split("]")[0].replace('"', "").replace(" ", "").split(","))
    assert got == {"systemd", "cpu", "meminfo", "loadavg", "filesystem", "diskstats", "pressure", "processes",
                   "zfs", "nfs", "nfsd", "mountstats", "textfile"}
    assert 'directory = "/var/lib/skmon-prod/alloy/textfile"' in c


def test_full_systemd_mode_keeps_the_full_config():
    c = cfg(agent_mode="systemd", agent_light=False)
    assert "prometheus.exporter.cadvisor" in c and "loki.write" in c and '"netdev"' in c


def test_light_mode_scrape_interval_60s_but_still_job_alloy_unix():
    j = jobs(CTX_WITH_LIGHT_HOST)["alloy-unix-light"]
    assert j["scrape_interval"] == "60s"
    assert {"target_label": "job", "replacement": "alloy-unix"} in j["relabel_configs"]  # NodeDown still covers it
    assert [t["labels"]["node"] for t in j["static_configs"]] == [B]


def test_light_host_only_in_the_light_job_and_never_cadvisor():
    j = jobs(CTX_WITH_LIGHT_HOST)
    nodes = {name: {t["labels"]["node"] for t in job["static_configs"]} for name, job in j.items() if "static_configs" in job
             and name.startswith("alloy-")}
    assert nodes["alloy-unix"] == {A} and nodes["alloy-cadvisor"] == {A}
    assert nodes["alloy-unix-light"] == {B} and B in nodes["alloy-self"]


def test_light_job_keeps_the_systemd_failed_filter():
    j = jobs(CTX_WITH_LIGHT_HOST)
    assert j["alloy-unix-light"]["metric_relabel_configs"] == j["alloy-unix"]["metric_relabel_configs"]


def test_no_light_job_when_no_host_is_light_and_no_unix_job_when_all_are():
    all_full = {"skmon_agent_targets": [t | {"light": False} for t in CTX_WITH_LIGHT_HOST["skmon_agent_targets"]]}
    all_light = {"skmon_agent_targets": [t | {"light": True} for t in CTX_WITH_LIGHT_HOST["skmon_agent_targets"]]}
    assert "alloy-unix-light" not in jobs(all_full)
    j = jobs(all_light)
    assert "alloy-unix" not in j and "alloy-cadvisor" not in j and "alloy-unix-light" in j


def test_binary_checksum_verified():
    t = task_named(AGENT_PLAY, "Download the pinned Alloy binary")
    assert t["get_url"]["checksum"].startswith("sha256:")
    assert "sha256_linux_" in t["get_url"]["checksum"] and "alloy-linux-" in t["get_url"]["url"]
    assert t["get_url"]["url"].startswith("https://github.com/grafana/alloy/releases/download/")


def test_pins_carry_both_architectures():
    b = PINS["binaries"]["alloy"]
    assert len(b["sha256_linux_amd64"]) == 64 and len(b["sha256_linux_arm64"]) == 64


def test_binary_install_layout():
    by = {t["name"]: t for t in tasks(AGENT_PLAY)}
    assert by["Install the versioned Alloy binary"]["copy"]["dest"] == "/usr/local/bin/skmon-alloy-{{ skmon_pins.binaries.alloy.version }}"
    link = by["Point the Alloy symlink at the pinned binary"]["file"]
    assert link["dest"] == "/usr/local/bin/skmon-alloy" and link["state"] == "link"
    assert by["Unpack the Alloy release"]["unarchive"]["remote_src"] is True


def test_unsupported_architecture_and_missing_unzip_fail_clearly():
    by = {t["name"]: t for t in tasks(AGENT_PLAY)}
    assert "aarch64" in by["Pick the Alloy release architecture"]["set_fact"]["skmon_agent_arch"]
    assert "x86_64 (amd64) and aarch64 (arm64) only" in " ".join(by["Fail on an architecture without an Alloy release"]["fail"]["msg"].split())
    assert "unzip" in by["Fail when unzip is missing"]["fail"]["msg"]
    assert not any("package" in t and "unzip" in str(t) for t in tasks(AGENT_PLAY))


def test_state_absent_removes_unit_and_binary():
    names = [t["name"] for t in tasks(AGENT_PLAY) if "state == 'absent'" in str(t.get("when"))]
    assert any("unit" in n for n in names) and any("binary" in n for n in names)


def test_state_absent_keeps_the_state_directory():
    assert "/var/lib/skmon-{{ env }}/alloy" not in str([t for t in tasks(AGENT_PLAY) if "state == 'absent'" in str(t.get("when"))])


def test_install_tasks_skip_absent_hosts():
    install = task_named(AGENT_PLAY, "Write the systemd agent unit")
    assert "skmon_agent_state != 'absent'" in install["when"] and "'systemd'" in install["when"]


def test_parked_stops_the_unit():
    t = task_named(AGENT_PLAY, "Stop the systemd agent while parked")
    assert t["systemd"]["state"] == "stopped" and "skmon_parked" in str(t["when"])


def test_unparked_start_enables_the_unit_again():
    t = task_named(AGENT_PLAY, "Start the systemd agent")
    assert t["systemd"]["state"] == "started" and t["systemd"]["enabled"] is True and "not skmon_parked" in t["when"]


def test_smart_timer_only_for_light_hosts():
    by = {t["name"]: t for t in tasks(AGENT_PLAY)}
    for name in ("Write the SMART textfile script", "Write the SMART textfile units", "Start the SMART textfile timer"):
        assert "skmon_agent_self.light" in by[name]["when"]
    t = render(CFG / "alloy/skmon-smart.timer.j2", extra_ctx={"inventory_hostname": "h"})
    assert "OnUnitActiveSec=5min" in t
    s = render(CFG / "alloy/skmon-smart.service.j2", extra_ctx={"inventory_hostname": "h"})
    assert "/var/lib/skmon-prod/alloy/textfile" in s and "Type=oneshot" in s


def test_smart_script_parses():
    subprocess.run(["bash", "-n", SMART_SCRIPT], check=True)


def test_smart_script_without_smartctl_writes_nothing(tmp_path):
    # PATH holds symlinks to the few tools the script needs, and no smartctl
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("bash", "mktemp", "mkdir", "rm", "dirname", "chmod", "mv", "awk", "grep"):
        found = subprocess.run(["bash", "-c", f"command -v {tool}"], capture_output=True, text=True).stdout.strip()
        if found:
            (bindir / tool).symlink_to(found)
    out = tmp_path / "out"
    r = subprocess.run(["/bin/bash", str(SMART_SCRIPT), str(out)], capture_output=True, text=True, env={"PATH": str(bindir)})
    assert r.returncode == 0 and "smartctl not found" in r.stderr and not out.exists()


def test_smart_script_writes_atomically_with_a_fake_smartctl(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "smartctl"
    fake.write_text(
        '#!/bin/sh\n'
        'case "$1" in\n'
        '  --scan) echo "/dev/sda -d scsi # x"; echo "/dev/sdb -d scsi # y" ;;\n'
        '  -H) if [ "$2" = /dev/sda ]; then echo "SMART overall-health self-assessment test result: PASSED"; '
        'else echo "SMART overall-health self-assessment test result: FAILED!"; exit 8; fi ;;\n'
        '  -A) echo "194 Temperature_Celsius 0x0022 100 100 000 Old_age Always - 34 (Min/Max 20/45)" ;;\n'
        'esac\n')
    fake.chmod(0o755)
    out = tmp_path / "out"
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    r = subprocess.run(["bash", str(SMART_SCRIPT), str(out)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    prom = (out / "smart.prom").read_text()
    assert 'node_smart_healthy{device="sda"} 1' in prom and 'node_smart_healthy{device="sdb"} 0' in prom
    assert 'node_smart_temperature_celsius{device="sda"} 34' in prom
    assert [p.name for p in out.iterdir()] == ["smart.prom"]  # no temp file left behind
