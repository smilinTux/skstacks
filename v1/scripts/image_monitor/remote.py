"""Run a command on a cluster node over ssh, optionally through a jump host.

Read-only by construction: every call site in this tool only ever runs
`docker service ls`, `docker service ps`, `docker service inspect`,
`docker ps` or `docker exec ... php -r 'echo PHP_VERSION;'` (and the
equivalent for other runtimes). Nothing here writes to a node, a service, an
image or a vault.
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

SSH_TIMEOUT_SECONDS = 20


@dataclass
class RemoteResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    unreachable: bool = False   # couldn't even reach the host (timeout/refused/dns), vs. the command itself failing


def _ssh_argv(host: str, remote_cmd: str, *, user: str | None = None, identity: str | None = None) -> list[str]:
    argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TIMEOUT_SECONDS}"]
    if identity:
        argv += ["-i", identity]
    target = f"{user}@{host}" if user else host
    argv += [target, remote_cmd]
    return argv


def run_remote(host: str, remote_cmd: str, *, jump: str | None = None, user: str | None = None,
               identity: str | None = None, timeout: float = SSH_TIMEOUT_SECONDS + 10,
               runner=subprocess.run) -> RemoteResult:
    """Run `remote_cmd` on `host`, optionally hopping through `jump` first.

    When `jump` is set, the whole single-hop ssh argv is quoted as one
    string and handed to the jump host's shell, so a double-hop (laptop ->
    nammgr1001) is a single `ssh laptop "ssh ... nammgr1001 '<cmd>'"`.
    """
    if jump:
        inner = _ssh_argv(host, remote_cmd, user=user, identity=identity)
        inner_str = " ".join(shlex.quote(a) for a in inner)
        argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TIMEOUT_SECONDS}", jump, inner_str]
    else:
        argv = _ssh_argv(host, remote_cmd, user=user, identity=identity)

    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return RemoteResult(ok=False, unreachable=True, stderr=f"timed out reaching {host}")

    if proc.returncode == 255:
        # ssh's own exit code for "couldn't connect at all" (vs. a remote
        # command failure, which propagates the remote exit code).
        return RemoteResult(ok=False, unreachable=True, stdout=proc.stdout, stderr=proc.stderr)
    return RemoteResult(ok=proc.returncode == 0, stdout=proc.stdout, stderr=proc.stderr)
