"""
skbloom flow — the sovereign install flow as an ordered list of named Steps.

This is the Kubefirst-mapped spine, but every action is deterministic skbloom/skrender
code driven by an injected command runner (so it's testable and the LLM never touches
the cluster). The flow:

    preflight → bootstrap-cluster (k3d) → install-eso → secret-backend → deploy:<svc>…
    → final-check

Each step is idempotent: `check()` short-circuits work that's already satisfied
(cluster exists, ESO installed, rollout ready), so re-runs resume cleanly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .steps import Step

# the default sovereign service set (descriptor paths relative to the v2 root)
DEFAULT_SERVICES = ["cloud/skfence", "core/skca", "core/sksso", "core/sksec"]


@dataclass
class Profile:
    name: str = "sovereign"
    cluster: str = "skbloom"
    services: list = field(default_factory=lambda: list(DEFAULT_SERVICES))
    platform: str = "k8s"             # k8s (k3d) | swarm
    secret_seed: list = field(default_factory=list)   # [{key,value}] for the fake store (demo/test)
    tls: bool = False                 # instant local HTTPS via a cert-manager CA
    domain: str = "sk.local"          # base domain for per-service ingress URLs (when tls)
    config_overrides: dict = field(default_factory=dict)   # {deployed_name: {KEY: value}} — fine-tune


def _base(path: str) -> str:
    return path.rstrip("/").split("/")[-1]


def _split(entry: str):
    """A service entry is 'core/sksso' or 'core/sksso=login' (vanity name).
    Returns (descriptor_path, deployed_name)."""
    if "=" in entry:
        path, _, vanity = entry.partition("=")
        return path.strip(), (vanity.strip() or _base(path))
    return entry, _base(entry)


def install_flow(profile: Profile, *, run, v2_root: str) -> list:
    """Build the ordered Steps. `run(cmd_list)` executes a command and returns
    (stdout, rc) — inject a fake in tests, the real subprocess runner in prod.
    Dispatches on `profile.platform` (k8s via k3d, or a single-node Swarm)."""
    if profile.platform == "swarm":
        return _swarm_flow(profile, run=run, v2_root=v2_root)
    out = (lambda cmd: run(cmd)[0])

    def cluster_exists() -> bool:
        return profile.cluster in out(["k3d", "cluster", "list"])

    def eso_installed() -> bool:
        return "external-secrets" in out(["helm", "list", "-n", "external-secrets"])

    steps = [
        Step("preflight", description="probe the box + check tools",
             run=lambda: run(["bash", "-c", "command -v k3d kubectl helm >/dev/null"])),
        Step("bootstrap-cluster", description="local k3d bootstrap cluster",
             run=lambda: run(["k3d", "cluster", "create", profile.cluster, "--wait"]),
             check=cluster_exists),
        Step("install-eso", description="External Secrets Operator (helm)",
             run=lambda: run(["helm", "upgrade", "--install", "external-secrets",
                              "external-secrets/external-secrets", "-n", "external-secrets",
                              "--create-namespace", "--set", "installCRDs=true", "--wait"]),
             check=eso_installed),
        Step("secret-backend", description="register the cluster secret store",
             run=lambda: run(["bash", "-c",
                              "kubectl apply -f - <<'EOF'\n" + _secret_store(profile.secret_seed) + "EOF"])),
    ]

    if profile.tls:                    # instant HTTPS — a cert-manager self-signed CA
        steps.append(Step("local-tls", description="cert-manager CA for *.sk.local HTTPS",
                          run=lambda: _setup_tls(run),
                          check=lambda: "cert-manager" in out(["helm", "list", "-n", "cert-manager"])))

    # one deploy step per service — render with skrender (vanity name), apply
    for svc in profile.services:
        path, name = _split(svc)
        dom = profile.domain if profile.tls else None      # ingress URL only when TLS is wired
        cfg = profile.config_overrides.get(name)            # fine-tune knobs for this service
        steps.append(Step(
            f"deploy:{name}", description=f"render + apply {name}" + (f" ({name}.{profile.domain})" if dom else ""),
            run=(lambda path=path, name=name, dom=dom, cfg=cfg: _deploy(run, v2_root, path, name, dom, cfg)),
            check=(lambda name=name: "Running" in out(
                ["kubectl", "get", "pods", "-n", name, "--no-headers"]) and
                "0/" not in out(["kubectl", "get", "pods", "-n", name, "--no-headers"]))))

    steps.append(Step("final-check", description="all workloads reconciled",
                      run=lambda: run(["bash", "-c", "kubectl get pods -A >/dev/null"])))
    return steps


def _swarm_flow(profile: Profile, *, run, v2_root: str) -> list:
    """A lean single-node Docker Swarm install. No ESO — Swarm injects secrets as env at
    `docker stack deploy` time (render_swarm emits ${ENV} refs)."""
    out = (lambda cmd: run(cmd)[0])
    steps = [
        Step("preflight", description="check docker is present",
             run=lambda: run(["bash", "-c", "command -v docker >/dev/null"])),
        Step("bootstrap-swarm", description="single-node swarm (docker swarm init)",
             run=lambda: run(["docker", "swarm", "init", "--advertise-addr", "127.0.0.1"]),
             check=lambda: "Swarm: active" in out(["docker", "info"])),
    ]
    for svc in profile.services:
        path, name = _split(svc)
        dom = profile.domain if profile.tls else None
        steps.append(Step(
            f"deploy:{name}", description=f"docker stack deploy {name}" + (f" ({name}.{profile.domain})" if dom else ""),
            run=(lambda path=path, name=name, dom=dom: _swarm_deploy(run, v2_root, path, name, profile.secret_seed, dom)),
            check=(lambda name=name: name in out(["docker", "stack", "ls"]))))
    steps.append(Step("final-check", description="services converged",
                      run=lambda: run(["bash", "-c", "docker service ls >/dev/null"])))
    return steps


def _swarm_deploy(run, v2_root: str, svc: str, name: str, seed, domain: str | None = None) -> None:
    import shlex
    import yaml
    from skrender.render import load_descriptor, render_swarm
    desc = load_descriptor(os.path.join(v2_root, svc, "app.yaml"))
    # single-node swarm → 1 replica per service; domain → Traefik HTTPS labels
    compose = yaml.safe_dump(render_swarm(desc, name=name, replicas=1, domain=domain))
    # Swarm interpolates ${VAR} from the env of the deploying CLI — export the seeded
    # secrets (env name = the part after 'scope/', uppercased) so they land in the service.
    exports = "; ".join(f"export {s['key'].split('/')[-1].upper()}={shlex.quote(str(s['value']))}"
                        for s in (seed or []))
    pre = (exports + "; ") if exports else ""
    script = (pre + f"cat > /tmp/skbloom-{name}.yml <<'EOF'\n{compose}\nEOF\n"
              f"docker stack deploy -c /tmp/skbloom-{name}.yml {name} --detach=true")
    out, rc = run(["bash", "-c", script])
    if rc != 0:
        raise RuntimeError(f"stack deploy {name} failed: {out.strip()[-200:]}")


def service_urls(profile: Profile, v2_root: str) -> dict:
    """{deployed_name: url} for each service that exposes a web UI (has ports).
    The one-stop-shop links the control-plane surfaces."""
    import yaml
    scheme = "https" if profile.tls else "http"
    out = {}
    for entry in profile.services:
        path = entry.split("=", 1)[0]
        name = entry.split("=", 1)[1] if "=" in entry else _base(entry)
        try:
            desc = yaml.safe_load(open(os.path.join(v2_root, path, "app.yaml"))) or {}
        except Exception:
            continue
        if (desc.get("deploy") or {}).get("ports"):
            out[name] = f"{scheme}://{name}.{profile.domain}"
    return out


def _setup_tls(run) -> None:
    import yaml
    from .tls import ca_bootstrap_manifests
    run(["helm", "repo", "add", "jetstack", "https://charts.jetstack.io"])
    run(["helm", "repo", "update"])
    run(["helm", "upgrade", "--install", "cert-manager", "jetstack/cert-manager",
         "-n", "cert-manager", "--create-namespace", "--set", "crds.enabled=true", "--wait"])
    man = yaml.safe_dump_all(ca_bootstrap_manifests())
    run(["bash", "-c", "kubectl apply -f - <<'EOF'\n" + man + "\nEOF"])


def _deploy(run, v2_root: str, svc: str, name: str, domain: str | None = None,
            config: dict | None = None) -> None:
    from skrender.render import load_descriptor, render_k8s
    import yaml
    desc = load_descriptor(os.path.join(v2_root, svc, "app.yaml"))
    # vanity name → object/URL names; domain → an HTTPS ingress; config → fine-tuned knobs
    manifest = yaml.safe_dump_all(render_k8s(desc, name=name, domain=domain, config=config))
    kind = "daemonset" if (desc.get("deploy") or {}).get("mode") == "global" else "deployment"
    run(["bash", "-c", f"kubectl create namespace {name} --dry-run=client -o yaml | kubectl apply -f -"])
    run(["bash", "-c", "kubectl apply -f - <<'EOF'\n" + manifest + "\nEOF"])
    # wait for it to actually roll out — the step isn't "done" until the workload is Ready
    out, rc = run(["bash", "-c", f"kubectl rollout status {kind}/{name} -n {name} --timeout=150s"])
    if rc != 0:
        raise RuntimeError(f"{name} did not roll out: {out.strip()[-200:]}")


def _secret_store(seed) -> str:
    import yaml
    data = [{"key": s["key"], "value": s["value"]} for s in (seed or [])]
    doc = {"apiVersion": "external-secrets.io/v1", "kind": "ClusterSecretStore",
           "metadata": {"name": "skstacks-backend"},
           "spec": {"provider": {"fake": {"data": data}}}}
    return yaml.safe_dump(doc)
