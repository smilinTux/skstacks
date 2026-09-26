"""gpg-signer's gunicorn binds IPv4 only (0.0.0.0:8080). busybox wget resolves
`localhost` to ::1 first and does not fall back, so a localhost probe is
refused forever: the skstack06 v2.18.0 run saw the container marked unhealthy
and SIGTERMed 72s after start. gunicorn then exits 0, and with
restart_policy on-failure Swarm never restarts a clean exit, so the service
sat at 0/1 for good. Probe the address it binds, and restart on any exit."""
import pathlib
import re

SKORCH = pathlib.Path(__file__).resolve().parents[1] / "ansible" / "optional" / "skorch"
COMPOSE = (SKORCH / "src" / "config" / "skorch" / "skorch.yml.j2").read_text()
DOCKERFILE = (SKORCH / "src" / "gpg-signer" / "Dockerfile").read_text()


def _gpg_block():
    return COMPOSE.split("\n  gpg-signer:\n", 1)[1].split("{% endif %}", 1)[0]


def _bind_host():
    return re.search(r'"-b", "([^:"]+):8080"', DOCKERFILE).group(1)


def test_compose_healthcheck_probes_the_bound_ipv4_address():
    assert _bind_host() == "0.0.0.0"
    assert "http://127.0.0.1:8080/health" in _gpg_block()
    assert "localhost" not in _gpg_block()


def test_dockerfile_healthcheck_probes_the_bound_ipv4_address():
    hc = DOCKERFILE.split("HEALTHCHECK", 1)[1].split("\n\n", 1)[0]
    assert "http://127.0.0.1:8080/health" in hc and "localhost" not in hc


def test_gpg_signer_restarts_after_a_clean_exit():
    assert re.search(r"restart_policy:\s*\n(\s*#.*\n)*\s*condition: any\n", _gpg_block())
