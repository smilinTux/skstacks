"""ClamAV memory tuning for skhub.

Measured on a throwaway local clamav/clamav:latest container (not a cluster): with the
image's stock clamd.conf (MaxThreads 10, ConcurrentDatabaseReload yes, the compiled-in
defaults), idle RSS was ~961 MiB and a forced `clamdscan --reload` spiked to ~1.86 GiB
(+94%) before settling back, because the old and new database are both resident during
the reload. With ConcurrentDatabaseReload set to "no" (the old database is freed before
the new one loads), idle RSS was ~956 MiB (unchanged -- that is mostly the resident
signature database) and the same reload never exceeded the idle baseline (it dipped to
~240 MiB then climbed back to ~954 MiB). MaxThreads bounds worst-case *concurrent*-scan
memory, which this idle+reload test does not exercise.

The clamav/clamav image has no env var for MaxThreads/StreamMaxLength/MaxScanSize/
ConcurrentDatabaseReload (only CLAMAV_NO_CLAMD/CLAMAV_NO_FRESHCLAMD/CLAMAV_NO_MILTERD/
CLAMD_STARTUP_TIMEOUT/FRESHCLAM_CHECKS are); the documented way to change them is a full
bind-mount over /etc/clamav/clamd.conf. So this adds a rendered clamd.conf template and
mounts it, plus parameterizes the clamav service's existing deploy.resources (previously
hardcoded 512M reservation / 2G limit) the same way redis's are already parameterized.
"""
import pathlib

import jinja2
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
COMPOSE = SKHUB / "src/config/skhub/skhub.yml.j2"
CLAMD_CONF = SKHUB / "src/config/skhub/clamd.conf.j2"
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}


def render_compose(**over):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    return env.from_string(COMPOSE.read_text()).render(app="skhub", env="prod", skhub=dict(BASE, **over),
                                                       fence_service_name="skfenceha")


def services(**over):
    return yaml.safe_load(render_compose(**over))["services"]


def render_clamd_conf(**over):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    return env.from_string(CLAMD_CONF.read_text()).render(app="skhub", env="prod", skhub=dict(BASE, **over))


def test_clamd_conf_template_exists():
    assert CLAMD_CONF.exists()


def test_clamd_conf_defaults():
    lines = [l for l in render_clamd_conf().splitlines() if l.strip()]
    assert "MaxThreads 2" in lines
    assert "StreamMaxLength 25M" in lines
    assert "MaxScanSize 100M" in lines
    assert "ConcurrentDatabaseReload no" in lines


def test_clamd_conf_keeps_existing_directives():
    """The directives the live config already sets must not be dropped by the
    switch from the image default to our rendered file."""
    text = render_clamd_conf()
    assert "LogFile /var/log/clamav/clamd.log" in text
    assert "LogTime yes" in text
    assert "LocalSocket /tmp/clamd.sock" in text
    assert "TCPSocket 3310" in text
    assert "User clamav" in text


def test_clamd_conf_max_threads_overridable():
    text = render_clamd_conf(CLAMD_MAX_THREADS=4)
    assert "MaxThreads 4" in text.splitlines()


def test_clamd_conf_concurrent_reload_overridable():
    text = render_clamd_conf(CLAMD_CONCURRENT_DATABASE_RELOAD=True)
    assert "ConcurrentDatabaseReload yes" in text.splitlines()


def test_clamd_conf_scan_size_overridable_and_stay_aligned():
    """StreamMaxLength is driven by the same *_MB knob the files_antivirus
    av_stream_max_length occ setting reads (test_skhub_files_antivirus_tuning),
    so the two cannot drift apart."""
    text = render_clamd_conf(CLAMD_STREAM_MAX_LENGTH_MB=50, CLAMD_MAX_SCAN_SIZE_MB=200)
    assert "StreamMaxLength 50M" in text.splitlines()
    assert "MaxScanSize 200M" in text.splitlines()


def test_clamav_service_mounts_clamd_conf():
    volumes = services()["clamav"]["volumes"]
    assert any(v.startswith("/var/data/config/skhub-prod/clamd.conf:/etc/clamav/clamd.conf") for v in volumes), volumes


def test_clamav_resources_default_unchanged():
    """Parameterizing must not change the values already proven in production."""
    res = services()["clamav"]["deploy"]["resources"]
    assert res["reservations"]["memory"] == "512M"
    assert res["limits"]["memory"] == "2G"


def test_clamav_resources_overridable():
    res = services(CLAMAV_RESOURCES_RESERVATIONS_MEMORY="256M",
                    CLAMAV_RESOURCES_LIMITS_MEMORY="1G")["clamav"]["deploy"]["resources"]
    assert res["reservations"]["memory"] == "256M"
    assert res["limits"]["memory"] == "1G"


def test_only_clamav_service_changes_with_clamav_knobs():
    a = services()
    b = services(CLAMAV_RESOURCES_LIMITS_MEMORY="1G")
    for name in a:
        if name != "clamav":
            assert a[name] == b[name], name
