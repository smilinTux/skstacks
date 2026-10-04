"""skfenceha's error-pages image is built once, by the deploy playbook,
on exactly one host: the manager selected by select_manager_node.yml (the
same host the playbook labels traefik.acme.master=true in this same play,
see deploy_skfenceha-{dev,staging,prod}.yml). The compose template placed
it with only `node.role == manager`, so Swarm was free to schedule the
service's task on any of the other managers, which never built the image:
"failed to resolve reference ... not found", rejected and rescheduled
every ~5s forever (observed 2026-10-03/04, skfenceha stage of the v2.25.0
release-gate run, hung 5h+18m twice; same class hit NAM 2026-10-01).

Fix: constrain error-pages to the same node.labels.traefik.acme.master
label traefik-acme already uses, since it is always the exact node that
just built the image. Also: the deploy script's own convergence poll made
raw, unbounded `docker service ls`/`inspect` calls, and never failed the
task even when the worker never converged -- defense in depth so a future
hang of this kind fails fast and visibly instead of blocking an ansible
task (and this release-gate run) for hours."""
import pathlib

import jinja2
import yaml

CFG = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skfenceha/src/config/skfenceha"
COMPOSE = CFG / "skfenceha.yml.j2"
DEPLOY_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skfenceha/src/skfenceha/deploy.j2"


def render_compose(**overrides):
    skfenceha = {"ACME_ENABLED": False}
    skfenceha.update(overrides)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(COMPOSE.read_text()).render(
        app="skfenceha", env="dev", skfenceha=skfenceha, cluster_name="test", domain="example.com"
    )
    return yaml.safe_load(out)


def render_deploy_script(**overrides):
    skfenceha = {"ACME_ENABLED": False}
    skfenceha.update(overrides)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string(DEPLOY_SCRIPT.read_text()).render(
        app="skfenceha", env="dev", skfenceha=skfenceha, cluster_name="test", domain="example.com"
    )


def test_error_pages_pinned_to_the_acme_master_label():
    constraints = render_compose()["services"]["error-pages"]["deploy"]["placement"]["constraints"]
    assert "node.role == manager" in constraints
    assert "node.labels.traefik.acme.master == true" in constraints


def test_error_pages_placement_matches_traefik_acme_placement():
    svcs = render_compose()["services"]
    assert set(svcs["error-pages"]["deploy"]["placement"]["constraints"]) == set(
        svcs["traefik-acme"]["deploy"]["placement"]["constraints"]
    )


def test_deploy_script_docker_status_polls_are_bounded():
    # Every raw `docker service ls`/`docker service inspect` call in the
    # convergence poll (from "Checking service health" to the end of the
    # script) is wrapped in `timeout` on the same line, so a Swarm manager
    # made slow/unresponsive by a scheduling retry storm cannot hang this
    # task forever (previously only the `docker service update --force`
    # calls had a timeout; the pre-deploy record_preexisting() existence
    # checks run before any stack is deployed and are out of scope here).
    script = render_deploy_script()
    poll_section = script[script.index("Checking service health") :]
    checked_any = False
    for line in poll_section.splitlines():
        stripped = line.strip()
        if "docker service ls" in stripped or "docker service inspect" in stripped:
            checked_any = True
            assert "timeout" in stripped, f"unbounded docker status call, wrap it in timeout: {stripped!r}"
    assert checked_any, "expected at least one docker service ls/inspect call in the poll section"


def test_deploy_script_convergence_failure_exits_nonzero():
    # A worker that never converges must fail the ansible shell task, not
    # fall through to "Deployment complete!" with an implicit exit 0.
    script = render_deploy_script()
    lines = script.splitlines()
    fail_idx = next(i for i, l in enumerate(lines) if "worker health check" in l)
    following = "\n".join(lines[fail_idx : fail_idx + 4])
    assert "exit 1" in following
