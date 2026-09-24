"""Descriptor → deploy parity: one app.yaml renders to BOTH a Swarm stack and K8s
manifests, with secrets/replicas/HA/networks/healthcheck wired consistently."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from skrender.render import (load_descriptor, render_swarm, render_k8s,
                             render_helm, render_operator, SecretRef)

_SKFENCE = Path(__file__).resolve().parents[2] / "cloud" / "skfence" / "app.yaml"


@pytest.fixture
def desc():
    return load_descriptor(_SKFENCE)


# ── Swarm ──────────────────────────────────────────────────────────────────────
def test_swarm_renders_the_container_with_image_and_ports(desc):
    stack = render_swarm(desc)
    svc = stack["services"]["traefik"]
    assert svc["image"] == "traefik:v3.3"
    assert "80:80" in svc["ports"] and "443:443" in svc["ports"]
    assert svc["command"] == ["--configFile=/etc/traefik/traefik.yml"]


def test_swarm_ha_means_replicas_and_spread(desc):
    svc = render_swarm(desc)["services"]["traefik"]
    assert svc["deploy"]["replicas"] == 3                     # min_replicas, ha=true
    assert svc["deploy"]["placement"]["max_replicas_per_node"] == 1   # spread for HA


def test_swarm_secrets_become_env_refs_not_plaintext(desc):
    svc = render_swarm(desc)["services"]["traefik"]
    env = svc["environment"]
    # cloudflare_dns_token → an interpolation ref, never a literal value
    assert any("cloudflare_dns_token" in e.lower() and "${" in e for e in env)
    assert not any("CHANGEME" in e or "secret" == e for e in env)


def test_swarm_config_becomes_plain_env(desc):
    env = render_swarm(desc)["services"]["traefik"]["environment"]
    assert any(e == "LOG_LEVEL=INFO" for e in env)


def test_config_override_lets_you_finetune_a_knob(desc):
    # the "fine-tune" capability — override a descriptor config value at deploy
    env = render_swarm(desc, config={"LOG_LEVEL": "DEBUG"})["services"]["traefik"]["environment"]
    assert "LOG_LEVEL=DEBUG" in env and "LOG_LEVEL=INFO" not in env
    # untouched knobs keep their descriptor default
    assert any(e == "CERT_RESOLVER=main" for e in env)


def test_k8s_config_override(desc):
    dep = next(m for m in render_k8s(desc, config={"LOG_LEVEL": "DEBUG"}) if m["kind"] == "Deployment")
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "LOG_LEVEL", "value": "DEBUG"} in env


def test_config_override_only_changes_named_keys(desc):
    # passing an override for a key NOT in the descriptor still applies (additive knob)
    env = render_swarm(desc, config={"NEW_KNOB": "x"})["services"]["traefik"]["environment"]
    assert "NEW_KNOB=x" in env


def test_swarm_networks_and_healthcheck_and_volumes(desc):
    stack = render_swarm(desc)
    svc = stack["services"]["traefik"]
    assert svc["healthcheck"]["test"] == ["CMD", "traefik", "healthcheck", "--ping"]
    assert "skfence-certs:/certs" in svc["volumes"]
    assert stack["networks"]["cloud-edge"]["external"] is True   # declared networks are external
    assert "skfence-certs" in stack["volumes"]                   # named volume hoisted to top level


# ── Kubernetes ──────────────────────────────────────────────────────────────────
def test_k8s_emits_deployment_service_and_externalsecret(desc):
    kinds = {m["kind"] for m in render_k8s(desc)}
    assert {"Deployment", "Service", "ExternalSecret"} <= kinds


def test_k8s_deployment_replicas_and_probe(desc):
    dep = next(m for m in render_k8s(desc) if m["kind"] == "Deployment")
    assert dep["spec"]["replicas"] == 3
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["image"] == "traefik:v3.3"
    assert c["livenessProbe"]["exec"]["command"] == ["traefik", "healthcheck", "--ping"]


def test_k8s_secrets_flow_through_eso_not_inline(desc):
    ms = render_k8s(desc)
    es = next(m for m in ms if m["kind"] == "ExternalSecret")
    keys = [d["secretKey"] for d in es["spec"]["data"]]
    assert "CLOUDFLARE_DNS_TOKEN" in keys                      # UPPERCASE env name
    # the remote lookup still uses the scoped lowercase path
    refs = [d["remoteRef"]["key"] for d in es["spec"]["data"]]
    assert "skfence/cloudflare_dns_token" in refs
    dep = next(m for m in ms if m["kind"] == "Deployment")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    # env comes from the synced k8s Secret via envFrom secretRef — never inline values
    assert any(ref.get("secretRef") for ref in c.get("envFrom", []))


def test_secret_env_var_is_identical_across_platforms(desc):
    # an app must see the SAME env var name whether deployed to Swarm or K8s
    sw_env = render_swarm(desc)["services"]["traefik"]["environment"]
    es = next(m for m in render_k8s(desc) if m["kind"] == "ExternalSecret")
    k8s_vars = {d["secretKey"] for d in es["spec"]["data"]}    # become env vars via envFrom
    assert "CLOUDFLARE_DNS_TOKEN" in k8s_vars
    assert any(e.startswith("CLOUDFLARE_DNS_TOKEN=") for e in sw_env)


def test_k8s_service_selector_matches_pod_labels(desc):
    # a Service whose selector doesn't match the pod labels routes to NOTHING (silent
    # outage). Assert they line up so the service actually works.
    ms = render_k8s(desc)
    dep = next(m for m in ms if m["kind"] == "Deployment")
    svc = next(m for m in ms if m["kind"] == "Service")
    pod_labels = dep["spec"]["template"]["metadata"]["labels"]
    assert svc["spec"]["selector"].items() <= pod_labels.items()
    # and the service targetPort matches a real container port
    cports = {p["containerPort"] for p in dep["spec"]["template"]["spec"]["containers"][0]["ports"]}
    assert all(p["targetPort"] in cports for p in svc["spec"]["ports"])


def test_k8s_ingress_with_auto_tls_when_a_domain_is_given(desc):
    ms = render_k8s(desc, domain="sk.local")
    ing = next(m for m in ms if m["kind"] == "Ingress")
    assert ing["spec"]["rules"][0]["host"] == "skfence.sk.local"
    # TLS secret + cert-manager annotation → the cert is auto-issued (ingress-shim)
    assert ing["spec"]["tls"][0]["secretName"] == "skfence-tls"
    assert ing["metadata"]["annotations"]["cert-manager.io/cluster-issuer"] == "skbloom-ca"
    backend = ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == "skfence" and backend["port"]["number"] in (80, 443)


def test_ingress_host_follows_the_vanity_name(desc):
    ing = next(m for m in render_k8s(desc, name="login", domain="sk.local") if m["kind"] == "Ingress")
    assert ing["spec"]["rules"][0]["host"] == "login.sk.local"
    assert ing["spec"]["tls"][0]["secretName"] == "login-tls"


def test_no_ingress_without_a_domain(desc):
    assert not any(m["kind"] == "Ingress" for m in render_k8s(desc))


def test_no_ingress_for_a_portless_service():
    assert not any(m["kind"] == "Ingress" for m in render_k8s(GLOBAL_DESC, domain="sk.local"))


def test_no_secrets_means_no_externalsecret_or_envfrom():
    d = {"name": "plain", "scope": "plain",
         "deploy": {"container": "c", "image": "nginx", "ports": ["80:80"]}}
    ms = render_k8s(d)
    assert not any(m["kind"] == "ExternalSecret" for m in ms)
    dep = next(m for m in ms if m["kind"] == "Deployment")
    assert "envFrom" not in dep["spec"]["template"]["spec"]["containers"][0]


def test_secret_ref_helper_uppercases_env_name():
    assert SecretRef("cloudflare_dns_token").env_name == "CLOUDFLARE_DNS_TOKEN"


# ── vanity name override: call the deployment whatever you want ──
def test_k8s_vanity_name_overrides_object_names_but_keeps_service_identity(desc):
    ms = render_k8s(desc, name="login")
    dep = next(m for m in ms if m["kind"] == "Deployment")
    svc = next(m for m in ms if m["kind"] == "Service")
    es = next(m for m in ms if m["kind"] == "ExternalSecret")
    # deployment/namespace/service/labels take the vanity name
    assert dep["metadata"]["name"] == "login" and dep["metadata"]["namespace"] == "login"
    assert svc["spec"]["selector"]["app"] == "login"
    assert dep["spec"]["template"]["metadata"]["labels"]["app"] == "login"
    # but the IMAGE is still skfence's, and the secret still resolves from the real scope
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"] == "traefik:v3.3"
    assert any(d["remoteRef"]["key"] == "skfence/cloudflare_dns_token" for d in es["spec"]["data"])
    assert es["metadata"]["name"] == "login"          # the synced Secret follows the vanity name


def test_swarm_vanity_name_is_the_service_key(desc):
    stack = render_swarm(desc, name="edge")
    assert "edge" in stack["services"]                # the swarm service is named 'edge'
    assert stack["services"]["edge"]["image"] == "traefik:v3.3"


def test_swarm_single_node_replicas_override(desc):
    # an HA service (min_replicas 3) on a SINGLE-node swarm should be 1 replica, no spread
    svc = render_swarm(desc, replicas=1)["services"]["traefik"]
    assert svc["deploy"]["replicas"] == 1
    assert "placement" not in svc["deploy"]           # single node → no one-per-node spread


def test_swarm_traefik_labels_when_a_domain_is_given(desc):
    # parity with the k8s ingress — a domain → Traefik routing labels + TLS on Swarm
    svc = render_swarm(desc, name="edge", domain="sk.local")["services"]["edge"]
    labels = svc["deploy"]["labels"]
    assert "traefik.enable=true" in labels
    assert any("Host(`edge.sk.local`)" in l for l in labels)     # vanity host
    assert any(l.endswith(".tls=true") for l in labels)
    assert any("loadbalancer.server.port=80" in l for l in labels)   # routes to a real port


def test_swarm_no_traefik_labels_without_a_domain(desc):
    assert "labels" not in render_swarm(desc)["services"]["traefik"]["deploy"]


# ── global mode (per-node agents like CrowdSec) ──
GLOBAL_DESC = {
    "name": "sksec", "scope": "sksec",
    "deploy": {"container": "crowdsec", "image": "crowdsecurity/crowdsec:latest",
               "mode": "global", "healthcheck_test": ["CMD", "cscli", "lapi", "status"]},
    "secrets": [{"key": "crowdsec_enrollment_key"}],
}


def test_global_mode_swarm_uses_mode_global_not_replicas():
    svc = render_swarm(GLOBAL_DESC)["services"]["crowdsec"]
    assert svc["deploy"]["mode"] == "global"
    assert "replicas" not in svc["deploy"]


def test_global_mode_k8s_is_a_daemonset():
    kinds = {m["kind"] for m in render_k8s(GLOBAL_DESC)}
    assert "DaemonSet" in kinds and "Deployment" not in kinds


def test_k8s_service_ports_are_named(desc):
    # K8s requires a name on each port when a Service exposes more than one (caught live)
    svc = next(m for m in render_k8s(desc) if m["kind"] == "Service")
    names = [p["name"] for p in svc["spec"]["ports"]]
    assert all(names) and len(set(names)) == len(names)        # present + unique
    assert all(len(n) <= 15 and n.islower() for n in names)    # valid DNS-label-ish


def test_portless_workload_emits_no_service():
    # sksec (CrowdSec agent) exposes no ports → a Service would be invalid; skip it
    kinds = [m["kind"] for m in render_k8s(GLOBAL_DESC)]
    assert "Service" not in kinds
    assert "DaemonSet" in kinds and "ExternalSecret" in kinds


def test_every_descriptor_with_a_deploy_block_renders(tmp_path=None):
    # parity guard: each app.yaml that declares deploy must render to BOTH platforms
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    rendered = 0
    for app in root.glob("*/*/app.yaml"):
        d = load_descriptor(app)
        if not d.get("deploy"):
            continue
        sw = render_swarm(d); ks = render_k8s(d)
        assert sw["services"] and len(ks) >= 2          # workload + ExternalSecret (+ Service if ports)
        kinds = {m["kind"] for m in ks}
        assert "ExternalSecret" in kinds and ({"Deployment", "DaemonSet"} & kinds)
        rendered += 1
    assert rendered >= 1                       # at least the pilot(s) render


# ── deploy.kind dispatch ──────────────────────────────────────────────────────────
def test_kind_defaults_to_container_unchanged(desc):
    # skfence has no deploy.kind → render_k8s still produces the container workload
    kinds = {m["kind"] for m in render_k8s(desc)}
    assert {"Deployment", "Service", "ExternalSecret"} <= kinds
    assert "HelmRelease" not in kinds


HELM_DESC = {
    "name": "skmon", "scope": "skmon",
    "deploy": {"kind": "helm",
               "chart": "kube-prometheus-stack",
               "repo": "https://prometheus-community.github.io/helm-charts",
               "version": "65.1.0",
               "values": {"grafana": {"enabled": True}}},
    "secrets": [{"key": "grafana_admin_password"}],
}

OPERATOR_DESC = {
    "name": "argocd", "scope": "argocd",
    "deploy": {"kind": "operator",
               "manifests": [
                   {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
                    "metadata": {"name": "root-app"},
                    "spec": {"project": "default"}},
                   {"apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject",
                    "metadata": {"name": "default", "namespace": "explicit-ns"},
                    "spec": {}},
               ]},
}


def test_helm_dispatch_emits_helmrelease_and_helmrepository():
    ms = render_helm(HELM_DESC)
    kinds = {m["kind"] for m in ms}
    assert {"HelmRelease", "HelmRepository"} <= kinds
    # render_k8s dispatches to render_helm on deploy.kind
    assert {m["kind"] for m in render_k8s(HELM_DESC)} == kinds


def test_helm_chart_repo_version_are_wired():
    ms = render_helm(HELM_DESC)
    repo = next(m for m in ms if m["kind"] == "HelmRepository")
    rel = next(m for m in ms if m["kind"] == "HelmRelease")
    assert repo["spec"]["url"] == "https://prometheus-community.github.io/helm-charts"
    assert repo["apiVersion"] == "source.toolkit.fluxcd.io/v1"
    assert rel["apiVersion"] == "helm.toolkit.fluxcd.io/v2"
    cs = rel["spec"]["chart"]["spec"]
    assert cs["chart"] == "kube-prometheus-stack"
    assert cs["version"] == "65.1.0"
    assert cs["sourceRef"] == {"kind": "HelmRepository", "name": "skmon", "namespace": "skmon"}
    assert rel["spec"]["values"]["grafana"]["enabled"] is True


def test_helm_optional_version_omitted_when_absent():
    d = {**HELM_DESC, "deploy": {k: v for k, v in HELM_DESC["deploy"].items() if k != "version"}}
    rel = next(m for m in render_helm(d) if m["kind"] == "HelmRelease")
    assert "version" not in rel["spec"]["chart"]["spec"]


def test_helm_vanity_name_honored():
    ms = render_helm(HELM_DESC, name="mon")
    rel = next(m for m in ms if m["kind"] == "HelmRelease")
    repo = next(m for m in ms if m["kind"] == "HelmRepository")
    assert rel["metadata"]["name"] == "mon" and rel["metadata"]["namespace"] == "mon"
    assert rel["spec"]["releaseName"] == "mon"
    assert repo["metadata"]["namespace"] == "mon"
    # but the secret scope still resolves from the REAL service
    es = next(m for m in ms if m["kind"] == "ExternalSecret")
    assert any(d["remoteRef"]["key"] == "skmon/grafana_admin_password" for d in es["spec"]["data"])
    assert es["metadata"]["name"] == "mon"


def test_helm_secrets_flow_through_eso_not_inline():
    ms = render_helm(HELM_DESC)
    es = next(m for m in ms if m["kind"] == "ExternalSecret")
    assert es["apiVersion"] == "external-secrets.io/v1"
    assert es["spec"]["secretStoreRef"]["name"] == "skstacks-backend"
    keys = [d["secretKey"] for d in es["spec"]["data"]]
    assert "GRAFANA_ADMIN_PASSWORD" in keys                  # UPPERCASE key
    # the chart's values reference the synced Secret, not an inline value
    rel = next(m for m in ms if m["kind"] == "HelmRelease")
    vf = rel["spec"]["valuesFrom"]
    assert any(v["kind"] == "Secret" and v["name"] == "skmon-secrets" for v in vf)
    assert all("password" not in str(rel["spec"].get("values", {})).lower() for _ in [0])


def test_helm_no_secrets_means_no_externalsecret():
    d = {"name": "x", "scope": "x",
         "deploy": {"kind": "helm", "chart": "c", "repo": "https://example/charts"}}
    ms = render_helm(d)
    assert not any(m["kind"] == "ExternalSecret" for m in ms)
    rel = next(m for m in ms if m["kind"] == "HelmRelease")
    assert "valuesFrom" not in rel["spec"]


def test_operator_passes_manifests_through_namespaced():
    ms = render_operator(OPERATOR_DESC)
    kinds = [m["kind"] for m in ms]
    assert "Application" in kinds and "AppProject" in kinds
    app = next(m for m in ms if m["kind"] == "Application")
    # raw manifest with no namespace gets stamped with the vanity name
    assert app["metadata"]["namespace"] == "argocd"
    assert app["spec"]["project"] == "default"             # body passes through untouched
    # an explicit namespace in the raw manifest is preserved
    proj = next(m for m in ms if m["kind"] == "AppProject")
    assert proj["metadata"]["namespace"] == "explicit-ns"
    # dispatch via render_k8s
    assert {m["kind"] for m in render_k8s(OPERATOR_DESC)} == set(kinds)


def test_operator_vanity_name_stamps_namespace():
    ms = render_operator(OPERATOR_DESC, name="gitops")
    app = next(m for m in ms if m["kind"] == "Application")
    assert app["metadata"]["namespace"] == "gitops"


def test_operator_does_not_mutate_descriptor():
    import copy
    snapshot = copy.deepcopy(OPERATOR_DESC)
    render_operator(OPERATOR_DESC)
    assert OPERATOR_DESC == snapshot                        # passthrough must not mutate input

