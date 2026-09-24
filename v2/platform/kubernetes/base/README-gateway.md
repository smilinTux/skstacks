# In-cluster ingress = Gateway API + Traefik

**Why:** `ingress-nginx` is **EOL March 2026** (no further CVE patches; its
sanctioned successor InGate was also retired). SKStacks standardizes in-cluster
ingress on the **Kubernetes Gateway API** with **Traefik v3** as the controller —
the same engine that runs the `skfence` edge, so edge → cluster is one
version-pinned ingress stack ("own the full vertical"). Envoy Gateway is the
documented alternative if you ever want it.

## Files
| File | Role |
|---|---|
| `traefik-helmchart.yaml` | Installs Traefik v3 (RKE2/k3s helm-controller), `kubernetesGateway` provider ON, legacy `kubernetesIngress` OFF, exposed as a `LoadBalancer` (MetalLB VIP). |
| `gateway.yaml` | `GatewayClass` (`traefik.io/gateway-controller`) + the shared `skstacks` Gateway (HTTP→HTTPS, HTTPS terminates the cert-manager wildcard). |
| `httproute-example.yaml` | Per-service `HTTPRoute` template — copy this instead of writing `Ingress` objects. |

## Migrating a service off Ingress
1. Replace its `kind: Ingress` with an `HTTPRoute` (see `httproute-example.yaml`)
   whose `parentRefs` point at the `skstacks` Gateway in the `traefik` namespace.
2. TLS is terminated centrally at the Gateway (`skstacks-wildcard-tls`), so the
   route no longer carries per-service TLS config.
3. Apply via the platform overlay (`kubectl apply -k platform/kubernetes/overlays/<env>`).

> k3s/k3d: we disable the *bundled* Traefik (`--disable=traefik`) and deploy our
> own pinned Traefik so the version + Gateway config are identical everywhere.
