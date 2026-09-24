"""
skrender — turn an `app.yaml` descriptor into a deployable Swarm stack AND K8s
manifests, from ONE source of truth (descriptor → deploy parity).

Wiring rules (consistent across platforms):
- HA: `ha: true` + `min_replicas: N` → N replicas, spread one-per-node (Swarm placement
  / inherent in K8s).  "if you need one, get two."
- secrets: NEVER inline. Swarm → `${ENV}` interpolation refs; K8s → an ESO ExternalSecret
  that syncs the backend into a k8s Secret, consumed via `envFrom: secretRef`.
- config → plain environment. networks → external overlays. healthcheck_test → the
  container healthcheck / liveness probe.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def load_descriptor(path) -> dict:
    import yaml
    return yaml.safe_load(Path(path).read_text())


@dataclass(frozen=True)
class SecretRef:
    key: str

    @property
    def env_name(self) -> str:
        return self.key.upper()

    @property
    def swarm_ref(self) -> str:               # KEY=${KEY}
        return f"{self.env_name}=${{{self.env_name}}}"


def _replicas(desc: dict) -> int:
    return int(desc.get("min_replicas", 1)) if desc.get("ha") else 1


def _named_volumes(volumes) -> dict:
    """Named volumes (not host bind mounts) get hoisted to the top-level volumes:."""
    out: dict = {}
    for v in volumes or []:
        src = str(v).split(":", 1)[0]
        if src and not src.startswith(("/", ".")):
            out[src] = None
    return out


# ── Swarm ────────────────────────────────────────────────────────────────────────
def _effective_config(desc: dict, override) -> dict:
    """Descriptor config merged with caller overrides (the web 'fine-tune' panel)."""
    return {**(desc.get("config") or {}), **(override or {})}


def render_swarm(desc: dict, *, name: str | None = None, replicas: int | None = None,
                 domain: str | None = None, config: dict | None = None) -> dict:
    dep = desc.get("deploy") or {}
    # vanity name: what you CALL this deployment (service key / URL). The image, config,
    # and secret scope stay the real service — only the user-facing name changes.
    container = name or dep.get("container") or desc["name"]

    env = [f"{k}={v}" for k, v in _effective_config(desc, config).items()]
    env += [SecretRef(s["key"]).swarm_ref for s in (desc.get("secrets") or [])]

    svc: dict = {"image": dep["image"], "environment": env,
                 "networks": list(desc.get("networks") or [])}
    if dep.get("command"):
        svc["command"] = dep["command"]
    if dep.get("ports"):
        svc["ports"] = dep["ports"]
    if dep.get("volumes"):
        svc["volumes"] = dep["volumes"]
    if dep.get("healthcheck_test"):
        hc = desc.get("healthcheck") or {}
        svc["healthcheck"] = {"test": dep["healthcheck_test"],
                              "interval": hc.get("interval", "30s"),
                              "timeout": hc.get("timeout", "5s"),
                              "retries": int(hc.get("retries", 3))}

    if dep.get("mode") == "global":            # per-node agent (CrowdSec, node-exporter…)
        deploy: dict = {"mode": "global", "restart_policy": {"condition": "any"}}
    elif replicas is not None:                 # explicit override (e.g. single-node swarm → 1)
        deploy = {"replicas": int(replicas), "restart_policy": {"condition": "any"}}
    else:
        deploy = {"replicas": _replicas(desc), "restart_policy": {"condition": "any"}}
        if desc.get("ha"):
            deploy["placement"] = {"max_replicas_per_node": 1}
    # Traefik routing labels → HTTPS URL (parity with the k8s ingress)
    ports = dep.get("ports") or []
    if domain and ports:
        host = f"{container}.{domain}"
        port0 = str(ports[0]).split(":")[0]
        deploy["labels"] = [
            "traefik.enable=true",
            f"traefik.http.routers.{container}.rule=Host(`{host}`)",
            f"traefik.http.routers.{container}.entrypoints=websecure",
            f"traefik.http.routers.{container}.tls=true",
            f"traefik.http.services.{container}.loadbalancer.server.port={port0}",
        ]
    svc["deploy"] = deploy

    return {
        "services": {container: svc},
        "networks": {n: {"external": True} for n in (desc.get("networks") or [])},
        "volumes": _named_volumes(dep.get("volumes")),
    }


# ── Kubernetes ────────────────────────────────────────────────────────────────────
def _external_secret(name: str, ns: str, secrets: list, scope: str,
                     secret_name: str | None = None) -> dict:
    """The ESO ExternalSecret that syncs the backend into a k8s Secret. Shared by every
    deploy kind so secret handling is identical (external-secrets.io/v1, secretStoreRef
    skstacks-backend, UPPERCASE keys, scoped lowercase remoteRef)."""
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "refreshInterval": "1h",
            "secretStoreRef": {"name": "skstacks-backend", "kind": "ClusterSecretStore"},
            "target": {"name": secret_name or f"{name}-secrets", "creationPolicy": "Owner"},
            "data": [{"secretKey": SecretRef(s["key"]).env_name,
                      "remoteRef": {"key": f"{scope}/{s['key']}"}}
                     for s in secrets],
        },
    }


def render_k8s(desc: dict, *, name: str | None = None, domain: str | None = None,
               issuer: str = "skbloom-ca", config: dict | None = None) -> list:
    """Dispatch on deploy.kind → container (default, single-container Deployment/DaemonSet)
    / helm (Flux HelmRelease + HelmRepository) / operator (raw CRD manifest passthrough)."""
    dep = desc.get("deploy") or {}
    kind = dep.get("kind", "container")
    if kind == "helm":
        return render_helm(desc, name=name)
    if kind == "operator":
        return render_operator(desc, name=name)
    return _render_k8s_container(desc, name=name, domain=domain, issuer=issuer, config=config)


def _render_k8s_container(desc: dict, *, name: str | None = None, domain: str | None = None,
                          issuer: str = "skbloom-ca", config: dict | None = None) -> list:
    # vanity name overrides object names/namespace/labels/URL; the secret SCOPE stays the
    # real service so its secrets still resolve from the backend.
    real = desc["name"]
    name = name or real
    dep = desc.get("deploy") or {}
    container = dep.get("container") or name
    ns = name
    secret_name = f"{name}-secrets"
    secrets = desc.get("secrets") or []
    scope = desc.get("scope", real)

    manifests_head = []
    if secrets:
        # secretKey is the UPPERCASE env name so envFrom exposes the SAME var the Swarm
        # render uses (${CLOUDFLARE_DNS_TOKEN}) — consistent app env across platforms.
        manifests_head.append(_external_secret(name, ns, secrets, scope, secret_name))

    container_spec: dict = {
        "name": container,
        "image": dep["image"],
        "ports": [{"containerPort": int(str(p).split(":")[-1])} for p in (dep.get("ports") or [])],
        "env": [{"name": k, "value": str(v)} for k, v in _effective_config(desc, config).items()],
    }
    if secrets:                                  # only wire envFrom when there are secrets
        container_spec["envFrom"] = [{"secretRef": {"name": secret_name}}]
    if dep.get("command"):
        container_spec["args"] = dep["command"]            # descriptor command = container args
    ht = dep.get("healthcheck_test")
    if ht:
        cmd = ht[1:] if ht and ht[0] == "CMD" else ht
        container_spec["livenessProbe"] = {"exec": {"command": cmd},
                                           "periodSeconds": 30, "timeoutSeconds": 5, "failureThreshold": 3}
        container_spec["readinessProbe"] = {"exec": {"command": cmd}, "periodSeconds": 10}

    labels = {"app": name}
    if dep.get("mode") == "global":            # per-node agent → DaemonSet
        workload = {
            "apiVersion": "apps/v1", "kind": "DaemonSet",
            "metadata": {"name": name, "namespace": ns, "labels": labels},
            "spec": {
                "selector": {"matchLabels": labels},
                "template": {"metadata": {"labels": labels},
                             "spec": {"containers": [container_spec]}},
            },
        }
    else:
        workload = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": ns, "labels": labels},
            "spec": {
                "replicas": _replicas(desc),
                "selector": {"matchLabels": labels},
                "template": {"metadata": {"labels": labels},
                             "spec": {"containers": [container_spec]}},
            },
        }
    deployment = workload
    manifests = manifests_head + [deployment]
    ports = dep.get("ports") or []
    if ports:                                   # a Service is only valid with ≥1 named port
        service = {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": ns, "labels": labels},
            "spec": {"selector": labels,
                     "ports": [{"name": f"p{str(p).split(':')[0]}",   # named (K8s requires it for multi-port)
                                "port": int(str(p).split(":")[0]),
                                "targetPort": int(str(p).split(":")[-1])} for p in ports]},
        }
        manifests.append(service)
        if domain:                              # reachable HTTPS URL + auto-issued cert
            host = f"{name}.{domain}"
            port0 = int(str(ports[0]).split(":")[0])
            manifests.append({
                "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
                "metadata": {"name": name, "namespace": ns, "labels": labels,
                             "annotations": {"cert-manager.io/cluster-issuer": issuer}},
                "spec": {
                    "tls": [{"hosts": [host], "secretName": f"{name}-tls"}],
                    "rules": [{"host": host, "http": {"paths": [{
                        "path": "/", "pathType": "Prefix",
                        "backend": {"service": {"name": name, "port": {"number": port0}}}}]}}],
                },
            })
    return manifests


# ── Helm (Flux GitOps: HelmRelease + HelmRepository) ───────────────────────────────
def render_helm(desc: dict, *, name: str | None = None) -> list:
    """deploy.kind == helm → a Flux HelmRelease (helm.toolkit.fluxcd.io/v2) + the
    HelmRepository (source.toolkit.fluxcd.io/v1) it pulls from. GitOps-native, no
    in-cluster helm binary. Secrets sync via the SAME ESO ExternalSecret as render_k8s;
    the chart's values reference that synced Secret via valuesFrom."""
    real = desc["name"]
    name = name or real
    ns = name
    dep = desc.get("deploy") or {}
    secrets = desc.get("secrets") or []
    scope = desc.get("scope", real)

    manifests: list = []
    if secrets:
        # the chart consumes the synced Secret by reference — never inline values.
        manifests.append(_external_secret(name, ns, secrets, scope))

    # the Helm repo Flux pulls the chart from (named after the vanity name for clarity).
    manifests.append({
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "HelmRepository",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"interval": "1h", "url": dep["repo"]},
    })

    chart_spec: dict = {"chart": dep["chart"],
                        "sourceRef": {"kind": "HelmRepository", "name": name, "namespace": ns}}
    if dep.get("version"):
        chart_spec["version"] = dep["version"]

    release: dict = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "interval": "10m",
            "releaseName": name,
            "targetNamespace": dep.get("namespace") or ns,
            "chart": {"spec": chart_spec},
        },
    }
    if dep.get("values"):
        release["spec"]["values"] = dep["values"]
    if secrets:
        # the chart's values reference the ESO-synced Secret (keys = UPPERCASE env names).
        release["spec"]["valuesFrom"] = [{
            "kind": "Secret", "name": f"{name}-secrets",
            "valuesKey": SecretRef(s["key"]).env_name,
            "targetPath": SecretRef(s["key"]).env_name,
        } for s in secrets]
    manifests.append(release)
    return manifests


# ── Operator (raw CRD / operator manifest passthrough) ─────────────────────────────
def render_operator(desc: dict, *, name: str | None = None) -> list:
    """deploy.kind == operator → apply the descriptor's `manifests` list as-is, stamping
    each with the vanity namespace. A simple passthrough for operator-managed CRDs
    (the operator itself reconciles the actual workload)."""
    real = desc["name"]
    name = name or real
    ns = name
    dep = desc.get("deploy") or {}
    secrets = desc.get("secrets") or []
    scope = desc.get("scope", real)

    manifests: list = []
    if secrets:
        manifests.append(_external_secret(name, ns, secrets, scope))
    for raw in dep.get("manifests") or []:
        m = dict(raw)                              # don't mutate the descriptor
        meta = dict(m.get("metadata") or {})
        meta.setdefault("namespace", ns)           # stamp namespace, keep an explicit one
        m["metadata"] = meta
        manifests.append(m)
    return manifests
