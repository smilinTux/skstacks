# skrender — descriptor → deploy parity

One `app.yaml` → a deployable **Swarm stack** *and* **K8s manifests**, from a single
source of truth. No more hand-maintaining the same service twice.

```bash
skrender cloud/skfence --platform swarm        # → docker-compose stack
skrender cloud/skfence --platform k8s          # → Deployment + Service + ExternalSecret
skrender core/sksec   --platform k8s -o platform/kubernetes/sksec/manifests.yaml
```

## Wiring rules (consistent across platforms)
- **HA**: `ha: true` + `min_replicas: N` → N replicas, spread one-per-node (mantra:
  *if you need one, get two*). `deploy.mode: global` → per-node agent (Swarm global /
  K8s **DaemonSet**) — e.g. CrowdSec.
- **Secrets**: never inline. Swarm → `${ENV}` interpolation refs; K8s → an ESO
  **ExternalSecret** that syncs the backend into a Secret, consumed via `envFrom`.
- **config** → plain env. **networks** → external overlays. **healthcheck_test** →
  container healthcheck + liveness/readiness probes.

## The `deploy:` block (added to app.yaml)
```yaml
deploy:
  container: traefik
  image: "traefik:v3.3"
  command: ["--configFile=/etc/traefik/traefik.yml"]
  ports: ["80:80", "443:443"]
  volumes: ["skfence-certs:/certs", "/var/run/docker.sock:/var/run/docker.sock:ro"]
  healthcheck_test: ["CMD", "traefik", "healthcheck", "--ping"]
  # mode: global   # for per-node agents
```

**Pilot slice:** `cloud/skfence` (Traefik, HA), `core/sksec` (CrowdSec, global),
`core/sksso` (Authentik, HA). A parity test renders every descriptor that declares a
`deploy:` block to both platforms, so drift fails CI.
