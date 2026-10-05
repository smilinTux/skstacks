"""The skfenceha deploy script's traefik-acme restart (`docker service
scale $ACME_SVC=0` then back to `$DESIRED`) had no `timeout` wrapper and no
`--detach`, so a scale call blocks until the new task converges with no
bound of its own -- unlike every other mutating docker call in this script
(the worker/socket-proxy/certs-dumper `docker service update --force` calls
all use `timeout "${SERVICE_UPDATE_TIMEOUT:-300}"`, and #140 bounded the
worker-role convergence poll).

Observed: the v2.26.0 release-gate run (lane 1, 2026-10-04) landed forgejo
+ qdrant on the same manager that holds `traefik.acme.master=true` (the
node error-pages/traefik-acme are pinned to), filling that 4096 MB VM to
within 8 MB of its reservations. The re-scaled traefik-acme task sat
Pending ("insufficient resources on 1 node"); the bare `docker service
scale` call blocked on it for 48+ minutes with no output at all (ansible's
`shell:` module buffers output until the task ends), hanging the ansible
task and the whole run. #140 closed this class for the worker role and the
per-call status polls; it missed these two scale calls, which is a gap in
the SAME incident class, not a new one.

Fix: `--detach=true` (scale returns immediately) plus `timeout
"${SERVICE_UPDATE_TIMEOUT:-300}"` as a backstop, then a bounded poll
(identical shape to the existing worker-role poll) that fails the deploy
within a few minutes with a clear message if the acme-master role never
converges, instead of the deploy script silently moving on and the hang
surfacing hours later."""
import pathlib

import jinja2

DEPLOY_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skfenceha/src/skfenceha/deploy.j2"


def render_deploy_script(**overrides):
    skfenceha = {"ACME_ENABLED": False}
    skfenceha.update(overrides)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string(DEPLOY_SCRIPT.read_text()).render(
        app="skfenceha", env="dev", skfenceha=skfenceha, cluster_name="test", domain="example.com"
    )


def _acme_restart_section(script):
    start = script.index("Restarting traefik-acme")
    end = script.index("Restarting traefik-worker")
    return script[start:end]


def test_acme_scale_calls_are_bounded():
    section = _acme_restart_section(render_deploy_script())
    checked_any = False
    for line in section.splitlines():
        stripped = line.strip()
        if "docker service scale" in stripped:
            checked_any = True
            assert "timeout" in stripped, f"unbounded docker service scale call: {stripped!r}"
            assert "--detach=true" in stripped, f"scale call must detach, not block the shell task: {stripped!r}"
    assert checked_any, "expected at least one docker service scale call in the acme restart section"


def test_acme_convergence_poll_exists_and_is_bounded():
    script = render_deploy_script()
    section = _acme_restart_section(script)
    # The poll that replaces the removed blocking wait: bounded docker
    # service ls calls checking the acme service's own replica count,
    # the same shape as the existing worker-role poll.
    assert "ACME_SVC" in section
    # Only the replica-count poll itself, not the pre-existing "does the
    # service exist at all" check (`docker service inspect` with no
    # timeout is fine there: it is a local metadata read, not a wait for
    # convergence, and record_preexisting() makes the same kind of call).
    poll_lines = [l.strip() for l in section.splitlines() if "docker service ls" in l]
    assert poll_lines, "expected a bounded status poll (docker service ls) in the acme restart section"
    for line in poll_lines:
        assert "timeout" in line, f"unbounded docker status call in acme poll: {line!r}"


def test_acme_convergence_failure_exits_nonzero():
    # A pending/never-converged acme-master role must fail the ansible
    # shell task with a clear message, not fall through silently.
    script = render_deploy_script()
    assert "acme-master role never converged" in script
    idx = script.index("acme-master role never converged")
    following = script[idx : idx + 200]
    assert "exit 1" in following
