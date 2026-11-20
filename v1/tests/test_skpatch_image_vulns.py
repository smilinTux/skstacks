"""skpatch image vulnerabilities (tools/skpatch_image_vulns.py): Trivy report
parsing from a recorded fixture, the per-digest cache, one image failing,
the ignore file, and the CLI through a fake ssh and a fake docker."""
import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

V1 = Path(__file__).resolve().parents[1]
ROLE = V1 / "ansible" / "optional" / "skpatch-hosts"
TOOL = ROLE / "tools" / "skpatch_image_vulns.py"
# `trivy image --quiet --format json --severity HIGH,CRITICAL --ignore-unfixed
# alpine:3.18.0`, Trivy 0.76.2 (the pinned image), recorded 2026-11-20;
# Description/References/CVSS trimmed.
FIXTURE = V1 / "tests" / "fixtures" / "trivy_image_alpine_3.18.0.json"

spec = importlib.util.spec_from_file_location("skpatch_image_vulns", TOOL)
iv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(iv)

NOW = dt.datetime(2026, 11, 20, 6, 15, tzinfo=dt.timezone.utc)
TTL = dt.timedelta(hours=24)
D1 = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64
SERVICES = {f"alpine:3.18.0@{D1}": ["skweb_app", "skweb_worker"], f"ghcr.io/example/broken:1@{D2}": ["skbroken_x"]}


def test_the_recorded_trivy_report_parses_to_fixable_rows_severest_first():
    rows = iv.parse_trivy(json.loads(FIXTURE.read_text()))
    assert len(rows) == 9
    assert [r["severity"] for r in rows] == ["CRITICAL"] * 3 + ["HIGH"] * 6
    assert rows[0] == {"id": "CVE-2022-48174", "severity": "CRITICAL", "package": "busybox", "installed": "1.36.0-r9",
                       "fixed": "1.36.1-r1", "target": "alpine:3.18.0 (alpine 3.18.0)", "title": rows[0]["title"]}
    assert rows[0]["title"].startswith("busybox: stack overflow")
    assert {r["package"] for r in rows} == {"busybox", "busybox-binsh", "ssl_client", "libcrypto3", "libssl3",
                                            "musl", "musl-utils"}


def test_unfixed_low_and_duplicate_findings_are_dropped_and_empty_results_are_fine():
    v = {"VulnerabilityID": "CVE-1", "PkgName": "p", "InstalledVersion": "1", "FixedVersion": "2", "Severity": "HIGH"}
    doc = {"Results": [
        {"Target": "os", "Vulnerabilities": [v, {**v, "FixedVersion": ""}, {**v, "VulnerabilityID": "CVE-2", "Severity": "LOW"}]},
        {"Target": "app/go.mod", "Vulnerabilities": [v]},  # the same finding in a second target
        {"Target": "empty", "Vulnerabilities": None},
        {"Target": "no-key"},
    ]}
    assert [r["id"] for r in iv.parse_trivy(doc)] == ["CVE-1"]
    assert iv.parse_trivy({}) == [] and iv.parse_trivy({"Results": None}) == []


def test_the_trivy_command_is_pinned_and_mounts_only_its_cache():
    cmd = iv.trivy_command("alpine:3.18.0", iv.TRIVY_IMAGE, Path("/c/trivy"))
    assert cmd[:5] == ["docker", "run", "--rm", "-v", "/c/trivy:/root/.cache"]
    assert iv.TRIVY_IMAGE.startswith("aquasec/trivy:0.76.2@sha256:") and len(iv.TRIVY_IMAGE.split("@sha256:")[1]) == 64
    assert cmd[5:] == [iv.TRIVY_IMAGE, "image", "--quiet", "--format", "json", "--severity", "HIGH,CRITICAL",
                       "--ignore-unfixed", "alpine:3.18.0"]
    assert "/var/run/docker.sock" not in " ".join(cmd)
    auth = iv.trivy_command("x", iv.TRIVY_IMAGE, Path("/c"), registry_auth=Path("/h/cfg.json"))
    assert "/h/cfg.json:/root/.docker/config.json:ro" in auth


def test_a_failed_scan_keeps_only_the_registry_answer():
    # recorded from Trivy 0.76.2 against an image that exists only on the nodes
    err = ("2026-11-20T15:01:02Z\tFATAL\tFatal error\trun error: image scan error: unable to find the specified image\n"
           "\t* docker error: unable to inspect the image: Cannot connect to the Docker daemon\n"
           "\t* containerd error: containerd socket not found: /run/containerd/containerd.sock\n"
           "\t* podman error: unable to initialize Podman client: no podman socket found\n"
           "\t* remote error: GET https://index.docker.io/v2/library/x/manifests/latest: UNAUTHORIZED: authentication required\n")
    assert iv.trivy_error(err) == ("GET https://index.docker.io/v2/library/x/manifests/latest: "
                                   "UNAUTHORIZED: authentication required")
    assert iv.trivy_error("plain failure\n") == "plain failure" and iv.trivy_error("") == "no error output"


def _scanner(calls, fail=()):
    def scan(ref):
        calls.append(ref)
        if any(f in ref for f in fail):
            raise RuntimeError("trivy exit 1: MANIFEST_UNKNOWN: manifest unknown")
        return iv.parse_trivy(json.loads(FIXTURE.read_text()))
    return scan


def test_one_image_failing_is_reported_not_fatal(tmp_path):
    calls = []
    s = iv.build_summary("lab", "mgr", SERVICES, tmp_path, iv.TRIVY_IMAGE, TTL, [], _scanner(calls, ["broken"]), NOW)
    assert len(calls) == 2
    ok, bad = s["images"]
    assert (ok["status"], ok["critical"], ok["high"], ok["services"]) == ("ok", 3, 6, ["skweb_app", "skweb_worker"])
    assert ok["digest"] == D1 and ok["cached"] is False and ok["scanned_at"] == NOW.isoformat(timespec="seconds")
    assert bad["status"] == "failed" and "manifest unknown" in bad["error"] and bad["vulns"] == []
    assert s["totals"] == {"images": 2, "scanned": 1, "failed": 1, "critical": 3, "high": 6, "ignored": 0}
    # a failure is never cached; a success is, by digest
    assert sorted(p.name for p in (tmp_path / "results").iterdir()) == ["a" * 64 + ".json"]


def test_a_rerun_inside_24_hours_scans_nothing_and_after_it_scans_again(tmp_path):
    calls = []
    iv.build_summary("lab", "mgr", SERVICES, tmp_path, iv.TRIVY_IMAGE, TTL, [], _scanner(calls, ["broken"]), NOW)
    calls.clear()
    s = iv.build_summary("lab", "mgr", SERVICES, tmp_path, iv.TRIVY_IMAGE, TTL, [], _scanner(calls),
                         NOW + dt.timedelta(hours=23))
    assert calls == [f"ghcr.io/example/broken:1@{D2}"]  # only the one that failed last time
    assert s["images"][0]["cached"] is True and s["images"][0]["critical"] == 3
    assert s["images"][0]["scanned_at"] == NOW.isoformat(timespec="seconds")
    calls.clear()
    iv.build_summary("lab", "mgr", SERVICES, tmp_path, iv.TRIVY_IMAGE, TTL, [], _scanner(calls),
                     NOW + dt.timedelta(hours=25))
    assert calls == [f"alpine:3.18.0@{D1}"]  # stale now; broken was cached at +23h
    # a new Trivy pin invalidates every cached result
    calls.clear()
    iv.build_summary("lab", "mgr", SERVICES, tmp_path, "aquasec/trivy:9@sha256:" + "c" * 64, TTL, [], _scanner(calls),
                     NOW + dt.timedelta(hours=25))
    assert len(calls) == 2


def test_the_cache_is_keyed_by_digest_so_a_retag_reuses_it():
    assert iv.cache_key(f"alpine:3.18.0@{D1}") == iv.cache_key(f"registry.example/alpine:latest@{D1}") == "a" * 64
    assert iv.cache_key("alpine:3.18.0").startswith("ref-") and iv.cache_key("alpine:3.18.0") != iv.cache_key("alpine:3.19")


def test_the_ignore_file_silences_by_id_and_image_glob(tmp_path):
    f = tmp_path / "ignore"
    f.write_text("# known false positives\nCVE-2022-48174 alpine:*   # busybox ash, not reachable\n"
                 "CVE-2025-26519\n\nCVE-2023-5363 other/*\n")
    rules = iv.load_ignores(f)
    assert rules == [("CVE-2022-48174", "alpine:*"), ("CVE-2025-26519", "*"), ("CVE-2023-5363", "other/*")]
    s = iv.build_summary("lab", "mgr", {f"alpine:3.18.0@{D1}": ["a"]}, tmp_path, iv.TRIVY_IMAGE, TTL, rules,
                         _scanner([]), NOW)
    i = s["images"][0]
    assert (i["critical"], i["high"], i["ignored"]) == (0, 4, 5)
    assert not {v["id"] for v in i["vulns"]} & {"CVE-2022-48174", "CVE-2025-26519"}
    assert s["totals"]["ignored"] == 5


# --- the CLI, through a fake ssh and a fake docker ---------------------------

def _bin(tmp_path, ssh_out, ssh_rc=0):
    b = tmp_path / "bin"
    b.mkdir()
    (tmp_path / "svc.out").write_text(ssh_out)
    (b / "ssh").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path}/ssh.argv\ncat {tmp_path}/svc.out\nexit {ssh_rc}\n")
    # fake docker: the fixture for alpine, a registry error for anything else
    (b / "docker").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {tmp_path}/docker.log\n"
                              f"for a; do last=$a; done\n"
                              f"case $last in alpine:*) cat {FIXTURE}; exit 0 ;; esac\n"
                              "echo 'FATAL  image scan error: unable to find the specified image' >&2; exit 1\n")
    for f in b.iterdir():
        f.chmod(0o755)
    return b


def _cli(tmp_path, bindir, *extra):
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "SKPW_SWARM_SSH": "mgr-node"}
    return subprocess.run([sys.executable, str(TOOL), "--estate", "lab", "--out", str(tmp_path / "out" / "lab.json"),
                           "--cache-dir", str(tmp_path / "cache"), *extra],
                          capture_output=True, text=True, env=env, timeout=60)


def test_cli_lists_services_scans_each_unique_image_once_and_writes_the_summary(tmp_path):
    b = _bin(tmp_path, f"skweb_app\talpine:3.18.0@{D1}\nskweb_worker\talpine:3.18.0@{D1}\n"
                       f"skbroken_x\tghcr.io/example/broken:1@{D2}\n\n")
    proc = _cli(tmp_path, b)
    assert proc.returncode == 1, proc.stderr  # one image failed: written, flagged
    s = json.loads((tmp_path / "out" / "lab.json").read_text())
    assert s["estate"] == "lab" and s["manager"] == "mgr-node" and s["error"] is None
    assert s["totals"] == {"images": 2, "scanned": 1, "failed": 1, "critical": 3, "high": 6, "ignored": 0}
    assert s["images"][1]["error"] == "trivy exit 1: FATAL  image scan error: unable to find the specified image"
    argv = (tmp_path / "ssh.argv").read_text()
    assert "BatchMode=yes" in argv and "mgr-node" in argv and "service ls --format" in argv
    assert "service update" not in argv and "service rm" not in argv
    log = (tmp_path / "docker.log").read_text().splitlines()
    assert len(log) == 2 and all(iv.TRIVY_IMAGE in l and "--ignore-unfixed" in l for l in log)
    assert "3 critical, 6 high" in proc.stdout
    # rerun: alpine comes from the cache, only the failure is tried again
    proc = _cli(tmp_path, b)
    assert len((tmp_path / "docker.log").read_text().splitlines()) == 3 and "(cached)" in proc.stdout


def test_cli_writes_the_error_when_the_service_list_fails(tmp_path):
    b = _bin(tmp_path, "", ssh_rc=255)
    proc = _cli(tmp_path, b)
    assert proc.returncode == 2 and "docker service ls failed (exit 255)" in proc.stderr
    s = json.loads((tmp_path / "out" / "lab.json").read_text())
    assert s["error"].startswith("docker service ls failed") and s["images"] == []
    assert not (tmp_path / "docker.log").exists()


def test_cli_refuses_an_unpinned_trivy_image(tmp_path):
    proc = _cli(tmp_path, _bin(tmp_path, ""), "--trivy-image", "aquasec/trivy:latest")
    assert proc.returncode == 2 and "pinned by digest" in proc.stderr


def test_the_image_units_run_before_the_digest_and_share_its_env():
    units = ROLE / "tools" / "systemd"
    svc = (units / "skpatch-image-vulns@.service").read_text()
    timer = (units / "skpatch-image-vulns@.timer").read_text()
    assert "EnvironmentFile=%h/.config/skpatch-watch/%i.env" in svc
    assert "skpatch_image_vulns.py" in svc and '--out "$STATE_DIRECTORY/%i.json"' in svc
    assert "StateDirectory=skpatch-hosts/image-vulns" in svc and "CacheDirectory=skpatch-hosts/image-vulns" in svc
    assert "SuccessExitStatus=1" in svc
    assert "OnCalendar=*-*-* 06:15:00" in timer
