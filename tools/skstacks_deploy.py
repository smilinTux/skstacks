#!/usr/bin/env python3
"""skstacks-deploy: one-command SKStacks v1 (swarm) service deploy with
automatic rollback.

Wraps the manual sequence every real prod deploy already follows (see
tools/README.md and the design doc this implements): take a per-service
lock, back up, snapshot, record the current docker service specs, run the
playbook, verify, and -- on failure -- restore only the services that
failed, unless the app declares it is schema_changing or runs outside
swarm (mode: compose), in which case nothing is auto-restored and the
manual steps are printed instead.

Dry run (no --execute) prints the plan and runs no subprocess at all, not
even a read-only one.

--execute re-execs this same script under `sk-lock` (service or --cluster
mode) with --locked-run; sk-lock execs the wrapped command on success and
never does on contention (exit 75), so lock contention running nothing is
exercised for real against a test's fake sk-lock, not mocked.

Exit codes: 0 success; 64 usage error (bad service/env name, or --locked-run
invoked without --execute and the internal lock sentinel -- refuses before
touching anything); 75 lock contention (passed through unchanged from
sk-lock); 1 deploy failed, auto-restore succeeded; 2 deploy failed,
auto-restore also failed (needs a human); 3 deploy failed, auto-restore not
applicable (schema_changing app, non-swarm mode, or a backup/hook failure
before the playbook ever ran) -- nothing was touched beyond what already
ran.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
# Overridable for tests only (a throwaway probe.yml tree); a real deploy
# always runs this script from inside the framework tree it belongs to.
FRAMEWORK_ROOT = Path(os.environ.get("SKSTACKS_DEPLOY_FRAMEWORK_ROOT", HERE.parent))

REQUIRED_PROBE_KEYS = {"services", "mode", "schema_changing"}
VALID_MODES = {"swarm", "compose"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# --locked-run runs the real mutating sequence with no lock check of its
# own (it trusts it is already running inside one). It is for internal
# re-exec only: main() refuses it unless both --execute is also set AND
# this env var is set to this exact value, which only the lock-wrapping
# code path below sets on the child process it hands to sk-lock. A bare
# `--locked-run` (typed by hand, or left over in a copied command line)
# must never run the mutating sequence outside a real lock.
LOCK_SENTINEL_ENV = "SKSTACKS_DEPLOY_LOCKED"
LOCK_SENTINEL_VALUE = "1"

DEFAULT_HOOK_TIMEOUT = 600
DEFAULT_DOCKER_TIMEOUT = 60
DEFAULT_PLAYBOOK_TIMEOUT = 7200


class DeployError(Exception):
    """An error with a specific process exit code attached."""

    def __init__(self, message: str, code: int = 3):
        super().__init__(message)
        self.code = code


def sanitize_purpose(raw: str) -> str:
    """sk-predeploy-snap's purpose argument must match [a-z0-9-]."""
    s = re.sub(r"[^a-z0-9-]+", "-", raw.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "deploy"


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise DeployError(f"{path}: expected a YAML mapping at the top level")
    return data


def load_probe(service: str, framework_root: Path = FRAMEWORK_ROOT) -> dict:
    path = framework_root / "v1" / "ansible" / "optional" / service / "probe.yml"
    if not path.is_file():
        raise DeployError(
            f"no probe.yml for '{service}' ({path}); every deployable app "
            "must declare one before skstacks-deploy will touch it"
        )
    probe = load_yaml(path)
    validate_probe(probe, path)
    probe.setdefault("http_checks", [])
    return probe


def validate_probe(probe: dict, path: Path) -> None:
    missing = REQUIRED_PROBE_KEYS - probe.keys()
    if missing:
        raise DeployError(f"{path}: missing required key(s) {sorted(missing)}")
    if not isinstance(probe["services"], list) or not probe["services"]:
        raise DeployError(f"{path}: 'services' must be a non-empty list")
    if probe["mode"] not in VALID_MODES:
        raise DeployError(f"{path}: 'mode' must be one of {sorted(VALID_MODES)}")
    if not isinstance(probe["schema_changing"], bool):
        raise DeployError(f"{path}: 'schema_changing' must be true/false")
    for chk in probe.get("http_checks") or []:
        for key in ("name", "path", "expect_status"):
            if key not in chk:
                raise DeployError(f"{path}: http_checks entry missing '{key}'")


def load_settings(instance_dir: Path) -> dict:
    return load_yaml(instance_dir / "skstacks-deploy.yml")


# ---------------------------------------------------------------------------
# subprocess helpers (every mutating or external call funnels through here)
# ---------------------------------------------------------------------------

def run_hook(template: str, timeout: int = DEFAULT_HOOK_TIMEOUT, **fmt) -> int:
    cmd = template.format(**fmt)
    try:
        return subprocess.run(["bash", "-c", cmd], timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print(f"  hook timed out after {timeout}s: {cmd}")
        return 124  # conventional shell "command timed out" exit code


def docker_inspect(full_name: str, timeout: int = DEFAULT_DOCKER_TIMEOUT) -> dict | None:
    """Returns the parsed spec dict, or None if the service does not exist
    (or the call times out -- treated the same as "nothing to record/verify
    from", never raised up as a crash)."""
    try:
        proc = subprocess.run(
            ["docker", "service", "inspect", full_name],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"  docker service inspect {full_name} timed out after {timeout}s")
        return None
    if proc.returncode != 0:
        return None
    arr = json.loads(proc.stdout)
    return arr[0] if arr else None


def docker_desired_replicas(spec: dict) -> int | None:
    try:
        return spec["Spec"]["Mode"]["Replicated"]["Replicas"]
    except (KeyError, TypeError):
        return None


def docker_running_count(full_name: str, timeout: int = DEFAULT_DOCKER_TIMEOUT) -> int:
    try:
        proc = subprocess.run(
            ["docker", "service", "ps", full_name,
             "--filter", "desired-state=running", "--format", "{{.CurrentState}}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"  docker service ps {full_name} timed out after {timeout}s")
        return 0
    if proc.returncode != 0:
        return 0
    return sum(1 for line in proc.stdout.splitlines() if line.strip().startswith("Running"))


def docker_rollback(full_name: str, timeout: int = DEFAULT_DOCKER_TIMEOUT) -> int:
    try:
        return subprocess.run(
            ["docker", "service", "update", "--rollback", full_name], timeout=timeout
        ).returncode
    except subprocess.TimeoutExpired:
        print(f"  docker service update --rollback {full_name} timed out after {timeout}s")
        return 124


def http_check_ok(base_url: str, check: dict) -> bool:
    url = base_url.rstrip("/") + check["path"]
    req = urllib.request.Request(url, method=check.get("method", "GET"))
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:  # noqa: BLE001 - any network failure is a failed check
        print(f"  http_check {check['name']}: ERROR {e}")
        return False
    ok = status == check["expect_status"]
    print(f"  http_check {check['name']}: {status} (want {check['expect_status']}) "
          f"{'OK' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# plan / execute
# ---------------------------------------------------------------------------

def full_service_name(service: str, env: str, name: str) -> str:
    return f"{service}-{env}_{name}"


def print_plan(service: str, env: str, instance_dir: Path, settings: dict,
               svc_settings: dict, probe: dict, cluster: bool, lock_timeout: int) -> None:
    print(f"DRY RUN: would deploy {service} (env={env}) instance={instance_dir}")
    lock_target = "--cluster" if cluster else service
    print(f"  lock: sk-lock {lock_target} -w {lock_timeout} -- ...")
    print(f"  backup: db_dump_hook={'yes' if svc_settings.get('db_dump_hook') else 'no'}, "
          f"snapshot_hook={'yes' if settings.get('snapshot_hook') else 'NONE (will abort unless allow_no_snapshot_hook: true)'}")
    print(f"  record: docker service inspect for {len(probe['services'])} service(s): "
          + ", ".join(full_service_name(service, env, n) for n in probe["services"]))
    playbook = FRAMEWORK_ROOT / "v1" / "ansible" / "optional" / service / f"deploy_{service}-{env}.yml"
    inventory = instance_dir / settings.get("inventory", "v1/ansible/shared/hosts")
    print(f"  playbook: ansible-playbook -i {inventory} {playbook}"
          + (f" --vault-password-file {settings['vault_password_file']}" if settings.get("vault_password_file") else "")
          + (f" -e target_manager_group={settings['manager_group']} -l {settings['manager_group']}" if settings.get("manager_group") else ""))
    print(f"  verify: replicas+running for {len(probe['services'])} service(s); "
          f"http_checks={[c['name'] for c in probe['http_checks']]}; "
          f"parity_hook={'yes' if svc_settings.get('parity_hook') else 'none'}")
    mode = probe["mode"]
    schema_changing = probe["schema_changing"]
    if schema_changing or mode != "swarm":
        why = "schema_changing" if schema_changing else f"mode={mode}"
        print(f"  on failure: NO auto-restore ({why}); manual restore steps only")
    else:
        print("  on failure: restore only the failing service(s) via "
              "docker service update --rollback, re-verify")
    print("No commands executed (dry run). Use --execute to run.")


def execute_locked(service: str, env: str, instance_dir: Path, settings: dict,
                    svc_settings: dict, probe: dict) -> int:
    hook_timeout = settings.get("hook_timeout", DEFAULT_HOOK_TIMEOUT)
    docker_timeout = settings.get("docker_timeout", DEFAULT_DOCKER_TIMEOUT)
    playbook_timeout = settings.get("playbook_timeout", DEFAULT_PLAYBOOK_TIMEOUT)

    run_dir = instance_dir / ".skstacks-deploy" / "runs" / utc_ts()
    run_dir.mkdir(parents=True, exist_ok=True)
    records_dir = run_dir / "records"
    records_dir.mkdir(exist_ok=True)
    backup_dir = run_dir / "backup"
    backup_dir.mkdir(exist_ok=True)

    print(f"== {service} deploy ({env}) run_dir={run_dir}")

    # --- backup phase ---
    db_dump_hook = svc_settings.get("db_dump_hook")
    if db_dump_hook:
        print("-- db_dump_hook")
        if run_hook(db_dump_hook, timeout=hook_timeout, backup_dir=str(backup_dir),
                    service=service, env=env) != 0:
            print("db_dump_hook FAILED (or timed out); aborting before the playbook")
            return 3

    snapshot_hook = settings.get("snapshot_hook")
    if not snapshot_hook:
        if not settings.get("allow_no_snapshot_hook", False):
            print("no snapshot_hook configured for this instance and "
                  "allow_no_snapshot_hook is not true; aborting")
            return 3
        print("no snapshot_hook configured (allow_no_snapshot_hook: true); skipping")
    else:
        purpose = sanitize_purpose(f"{service}-{utc_ts()}")
        print(f"-- snapshot_hook (purpose={purpose})")
        if run_hook(snapshot_hook, timeout=hook_timeout, purpose=purpose) != 0:
            print("snapshot_hook FAILED (or timed out); aborting before the playbook")
            return 3

    # --- record phase ---
    print("-- record")
    records: dict[str, dict] = {}
    for name in probe["services"]:
        full = full_service_name(service, env, name)
        spec = docker_inspect(full, timeout=docker_timeout)
        if spec is None:
            print(f"  {full}: absent (first deploy, nothing to record)")
            continue
        records[name] = spec
        (records_dir / f"{name}.json").write_text(json.dumps(spec, indent=2))
        print(f"  {full}: recorded")

    # --- playbook phase ---
    print("-- playbook")
    playbook = FRAMEWORK_ROOT / "v1" / "ansible" / "optional" / service / f"deploy_{service}-{env}.yml"
    inventory = instance_dir / settings.get("inventory", "v1/ansible/shared/hosts")
    cmd = ["ansible-playbook", "-i", str(inventory), str(playbook)]
    vault_pw = settings.get("vault_password_file")
    if vault_pw:
        cmd += ["--vault-password-file", str(Path(vault_pw).expanduser())]
    manager_group = settings.get("manager_group")
    if manager_group:
        cmd += ["-e", f"target_manager_group={manager_group}", "-l", manager_group]
    try:
        playbook_rc = subprocess.run(cmd, cwd=instance_dir, timeout=playbook_timeout).returncode
    except subprocess.TimeoutExpired:
        print(f"playbook TIMED OUT after {playbook_timeout}s")
        playbook_rc = 124
    playbook_failed = playbook_rc != 0
    if playbook_failed:
        print(f"playbook FAILED rc={playbook_rc}")

    # --- verify phase ---
    print("-- verify")
    failing: set[str] = set()
    if playbook_failed:
        # The playbook itself failed: every declared service is suspect,
        # whether or not it had a prior recorded spec (a first deploy may
        # have recorded nothing at all).
        failing |= set(probe["services"])
    else:
        for name in probe["services"]:
            full = full_service_name(service, env, name)
            # Always re-inspect AFTER the playbook: a playbook can
            # intentionally change a service's desired replica count (a
            # scale-up/down), so comparing against the pre-deploy recorded
            # spec would flag a successful, intentional change as a
            # failure and trigger an unwarranted restore. The pre-deploy
            # `records` are for restoring TO on failure, not for judging
            # the post-deploy state.
            spec = docker_inspect(full, timeout=docker_timeout)
            if spec is None:
                continue
            desired = docker_desired_replicas(spec)
            if desired is None:
                continue  # job-mode or unknown mode service: nothing to count
            running = docker_running_count(full, timeout=docker_timeout)
            ok = running == desired
            print(f"  {full}: {running}/{desired} running {'OK' if ok else 'FAIL'}")
            if not ok:
                failing.add(name)

        base_url = svc_settings.get("base_url")
        for chk in probe["http_checks"]:
            if not base_url:
                print(f"  http_check {chk['name']}: no base_url configured for {service}; FAIL")
                failing |= set(probe["services"])
                continue
            if not http_check_ok(base_url, chk):
                failing |= set(probe["services"])

        parity_hook = svc_settings.get("parity_hook")
        if parity_hook:
            print("-- parity_hook")
            if run_hook(parity_hook, timeout=hook_timeout, service=service, env=env,
                        instance=str(instance_dir)) != 0:
                print("  parity_hook FAILED (or timed out)")
                failing |= set(probe["services"])

    if not failing:
        print(f"SUCCESS: {service} verified healthy. run_dir={run_dir}")
        return 0

    print(f"FAILED services: {sorted(failing)}")
    auto_restore = probe["mode"] == "swarm" and not probe["schema_changing"]
    if not auto_restore:
        why = "schema_changing" if probe["schema_changing"] else f"mode={probe['mode']}"
        print(f"auto-restore not applicable ({why}); manual restore steps:")
        for name in sorted(failing):
            full = full_service_name(service, env, name)
            rec = records_dir / f"{name}.json"
            if rec.exists():
                print(f"  {full}: recorded spec at {rec}; "
                      f"to roll back by hand: docker service update --rollback {full}")
            else:
                print(f"  {full}: no recorded spec (was absent before this deploy)")
        print(f"backup_dir={backup_dir}")
        return 3

    print("auto-restoring failing service(s):")
    still_failing: set[str] = set()
    for name in sorted(failing):
        full = full_service_name(service, env, name)
        if name not in records:
            print(f"  {full}: no recorded spec, cannot auto-restore")
            still_failing.add(name)
            continue
        rc = docker_rollback(full, timeout=docker_timeout)
        if rc != 0:
            print(f"  {full}: rollback FAILED rc={rc}")
            still_failing.add(name)
            continue
        desired = docker_desired_replicas(records[name])
        running = docker_running_count(full, timeout=docker_timeout)
        ok = desired is None or running == desired
        print(f"  {full}: rolled back, re-verify {running}/{desired} {'OK' if ok else 'STILL FAILING'}")
        if not ok:
            still_failing.add(name)

    print(f"run_dir={run_dir}")
    if still_failing:
        print(f"UNRECOVERED: {sorted(still_failing)} still failing after restore; evidence above, needs a human")
        return 2
    print("deploy FAILED but all affected service(s) were restored to their recorded spec")
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skstacks-deploy")
    p.add_argument("service")
    p.add_argument("--instance", required=True)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--cluster", action="store_true")
    p.add_argument("--env")
    p.add_argument("--lock-timeout", type=int)
    p.add_argument("--locked-run", action="store_true", help=argparse.SUPPRESS)
    return p


def main(argv: list[str]) -> int:
    args = build_arg_parser().parse_args(argv)

    # --locked-run runs the real mutating sequence with no lock check of
    # its own: it must only ever be reached via the sk-lock re-exec this
    # same function builds below, never typed by hand or left over in a
    # copied command line. Refuse before touching anything else (probe,
    # settings, filesystem) if the proof-of-lock sentinel is missing.
    if args.locked_run and not (args.execute and os.environ.get(LOCK_SENTINEL_ENV) == LOCK_SENTINEL_VALUE):
        print("error: --locked-run is internal (re-exec only) and refuses to run without "
              "--execute and the lock sentinel; refusing to run anything.", file=sys.stderr)
        return 64

    if not NAME_RE.match(args.service):
        print(f"error: invalid service name {args.service!r} (must match {NAME_RE.pattern})",
              file=sys.stderr)
        return 64

    instance_dir = Path(args.instance).resolve()

    try:
        probe = load_probe(args.service)
        settings = load_settings(instance_dir)
    except DeployError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code

    env = args.env or settings.get("env", "prod")
    if not NAME_RE.match(env):
        print(f"error: invalid env {env!r} (must match {NAME_RE.pattern})", file=sys.stderr)
        return 64
    svc_settings = (settings.get("services") or {}).get(args.service, {})

    if args.locked_run:
        try:
            return execute_locked(args.service, env, instance_dir, settings, svc_settings, probe)
        except DeployError as e:
            print(f"error: {e}", file=sys.stderr)
            return e.code

    lock_timeout = args.lock_timeout or settings.get("lock_timeout", 14400)

    if not args.execute:
        print_plan(args.service, env, instance_dir, settings, svc_settings, probe,
                    args.cluster, lock_timeout)
        return 0

    lock_target = ["--cluster"] if args.cluster else [args.service]
    cmd = ["sk-lock", *lock_target, "-w", str(lock_timeout), "--",
           sys.executable, str(Path(__file__).resolve()), args.service,
           "--instance", str(instance_dir), "--execute", "--locked-run", "--env", env]
    child_env = {**os.environ, LOCK_SENTINEL_ENV: LOCK_SENTINEL_VALUE}
    return subprocess.run(cmd, env=child_env).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
