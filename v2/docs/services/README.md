# SKStacks v2 — Service Catalog

28 services, grouped by 4C layer. **Status:** ✅ deploy-ready (has a validated `deploy:` block, renders to Swarm compose + K8s manifests via `skrender`) · 📋 descriptor-only (capability descriptor present, `deploy:` block TODO).

**Totals:** 10 ✅ deploy-ready · 18 📋 descriptor-only.

Every service resolves secrets from the secret backend (`skvault` / OpenBao) at deploy time — Swarm via `${ENV}`, K8s via an ESO `ExternalSecret` (`external-secrets.io/v1`) → `Secret` → `envFrom` (UPPERCASE env names). Descriptors contain **structure only, never secret values**.

## core

| Service | Capability | Provider | Status | Doc |
|---------|------------|----------|--------|-----|
| capauth | Sovereign M2M auth — PGP identity & secret access without OAuth | CapAuth (sovereign PGP) | ✅ | [capauth.md](capauth.md) |
| skca | Internal PKI / mTLS — ACME, X.509 mTLS, SSH CA | step-ca | ✅ | [skca.md](skca.md) |
| sksec | Threat defense — reputation/runtime/host/network IDS | CrowdSec + Falco + Wazuh + Suricata | ✅ | [sksec.md](sksec.md) |
| sksso | Identity / SSO — human SSO + agent service-auth | Authentik (Zitadel upgrade path) | ✅ | [sksso.md](sksso.md) |
| skvault | Secrets management — pluggable backend | OpenBao (HA Raft) / vault-file / CapAuth | ✅ | [skvault.md](skvault.md) |
| skwaf | Web application firewall — OWASP Top 10 at ingress | Coraza + OWASP CRS v4 (Traefik WASM) | 📋 | [skwaf.md](skwaf.md) |

## cloud

| Service | Capability | Provider | Status | Doc |
|---------|------------|----------|--------|-----|
| skfence | Edge / ingress — TLS, rate limiting, socket proxy | Traefik v3 + Coraza WAF | ✅ | [skfence.md](skfence.md) |
| skcicd | CI/CD — image build + GitOps deployment | Forgejo Actions + Ansible + ArgoCD | 📋 | [skcicd.md](skcicd.md) |
| skdns | Sovereign DNS — authoritative + resolver/filter | Technitium | 📋 | [skdns.md](skdns.md) |
| skdweb | Decentralized availability + sovereign naming | IPFS + IPFS-Cluster + HNS/ENS | 📋 | [skdweb.md](skdweb.md) |
| skinfra | Infrastructure provisioning — IaC VM/network | OpenTofu | 📋 | [skinfra.md](skinfra.md) |
| skmesh | Overlay mesh / tunnels — sovereign WireGuard | Netbird (Tailscale bootstrap; Pangolin) | 📋 | [skmesh.md](skmesh.md) |

## comms

| Service | Capability | Provider | Status | Doc |
|---------|------------|----------|--------|-----|
| skbus | Machine A2A event bus — durable pub/sub | NATS JetStream | ✅ | [skbus.md](skbus.md) |
| skchat | Human/agent chat + federation — Matrix E2EE | Tuwunel (Synapse fallback) | 📋 | [skchat.md](skchat.md) |
| skcomms | Multi-channel outbound transport (17 paths) | skcomms (sovereign, → NATS skbus) | 📋 | [skcomms.md](skcomms.md) |
| skvoice | Voice/video RTC — SFU agent-in-call | LiveKit (+ Agents: Whisper/Chatterbox/Ollama) | 📋 | [skvoice.md](skvoice.md) |

## compute

| Service | Capability | Provider | Status | Doc |
|---------|------------|----------|--------|-----|
| skcache | Cache / KV store — ephemeral, session, pub/sub | Valkey (DragonflyDB at scale) | ✅ | [skcache.md](skcache.md) |
| skmodel | LLM / inference serving | Ollama (vLLM at scale) | ✅ | [skmodel.md](skmodel.md) |
| skobject | Object / S3 storage | Garage (SeaweedFS alt) | ✅ | [skobject.md](skobject.md) |
| skbackup | Backup — encrypted dedup snapshots to object | Restic (→ Garage S3) | 📋 | [skbackup.md](skbackup.md) |
| skblock | Block storage — replicated PV (RWO) | Longhorn | 📋 | [skblock.md](skblock.md) |
| skdata | Relational + vector + graph + FTS Postgres | PostgreSQL 17 (pgvector + pg_search + AGE) | 📋 | [skdata.md](skdata.md) |
| skfile | Shared file storage — POSIX RWX | JuiceFS CE (→ skobject + skdata) | 📋 | [skfile.md](skfile.md) |
| skfiles | File sync and collaboration | Nextcloud + Syncthing | 📋 | [skfiles.md](skfiles.md) |
| skflow | Automation / workflow — agent + human-in-loop | Windmill | 📋 | [skflow.md](skflow.md) |
| skmon | Observability — metrics, logs, traces (OTel) | Prometheus + Grafana + VictoriaLogs + Tempo + Alloy | 📋 | [skmon.md](skmon.md) |
| skpulse | Uptime / status monitoring | Gatus + Blackbox Exporter | 📋 | [skpulse.md](skpulse.md) |

## apps

| Service | Capability | Provider | Status | Doc |
|---------|------------|----------|--------|-----|
| skturn | WebRTC media relay — TURN/STUN NAT traversal | coturn 4.6 | 📋 | [skturn.md](skturn.md) |
