"""Probe a runtime version inside a running service's container.

Two steps, both read-only: find which node the service's task is currently
running on (`docker service ps`), then on that node find the container
(`docker ps`) and `docker exec` the runtime's probe command inside it
(e.g. `php -r 'echo PHP_VERSION;'`). Despite the module name this runs
whatever probe `floors.yaml` configures for a runtime, not only PHP.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass

from remote import run_remote

_PS_NODE_CMD_TMPL = "sudo docker service ps {svc} --filter desired-state=running --format '{{{{.Node}}}}'"
_PS_CONTAINER_CMD_TMPL = "sudo docker ps --filter name={svc} --format '{{{{.ID}}}}'"


@dataclass
class ProbeResult:
    output: str | None = None     # combined stdout from the probe command, or None on failure
    error: str | None = None


def probe_runtime(service: str, probe_argv: list[str], *, manager: str, jump: str | None, user: str | None,
                  identity: str | None, runner=None) -> ProbeResult:
    import subprocess
    runner = runner or subprocess.run

    node_result = run_remote(manager, _PS_NODE_CMD_TMPL.format(svc=service), jump=jump, user=user,
                             identity=identity, runner=runner)
    if not node_result.ok:
        return ProbeResult(error=f"could not find running task for {service}: {node_result.stderr.strip()}")
    nodes = [n.strip() for n in node_result.stdout.splitlines() if n.strip()]
    if not nodes:
        return ProbeResult(error=f"{service} has no running task")
    node = nodes[0]

    ps_result = run_remote(node, _PS_CONTAINER_CMD_TMPL.format(svc=service), jump=jump, user=user,
                           identity=identity, runner=runner)
    if not ps_result.ok:
        return ProbeResult(error=f"could not reach node {node} for {service}: {ps_result.stderr.strip()}")
    container_ids = [c.strip() for c in ps_result.stdout.splitlines() if c.strip()]
    if not container_ids:
        return ProbeResult(error=f"no container found for {service} on {node}")
    container_id = container_ids[0]

    exec_cmd = "sudo docker exec " + container_id + " " + " ".join(shlex.quote(a) for a in probe_argv)
    exec_result = run_remote(node, exec_cmd, jump=jump, user=user, identity=identity, runner=runner)
    if not exec_result.ok:
        # Covers both "no such runtime in this container" (e.g. a non-PHP
        # image accidentally matched) and a transient exec failure; either
        # way this is a skip for this service, not a crash of the run.
        return ProbeResult(error=(exec_result.stderr or "probe command failed").strip())
    return ProbeResult(output=exec_result.stdout.strip())
