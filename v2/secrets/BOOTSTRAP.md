# SKStacks v2 — Secret Bootstrap (No Catch-22)

The hard problem with any secret backend is the **chicken-and-egg**: services
need a secret to authenticate to the secret store, and the secret store's own
unseal keys / root token are themselves secrets. If you get this wrong you
either (a) leave plaintext keys on disk, or (b) lock yourself out when the
server is down. SKStacks solves it with a layered root of trust.

## The layers

```
┌──────────────────────────────────────────────────────────────────────────┐
│  ROOT OF TRUST  (server-less, offline, depends on NOTHING)                 │
│                                                                            │
│   capauth      operator PGP private key (Ed25519/RSA-4096)  ── the ONE     │
│                irreducible human secret                                    │
│   vault-file   ansible-vault AES-256 files + a password file (mode 600)    │
│                                                                            │
│   ↳ Either can fully operate with no server running. This is also the      │
│     factory default (`_DEFAULT_BACKEND = "vault-file"`), so day-0 deploys  │
│     and recovery never require a secret server to already be up.           │
└──────────────────────────────────────────────────────────────────────────┘
                                   │  bootstraps
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  RUNTIME BACKEND   OpenBao (HA Raft)  — the default *server* backend       │
│                                                                            │
│   `bao operator init -pgp-keys=<capauth pubkeys> -root-token-pgp-key=...`  │
│   → unseal-key shares AND root token come out ALREADY PGP-ENCRYPTED.       │
│     The init JSON contains NO plaintext. It is safe to store in vault-file │
│     / commit. Only a capauth PGP private key can decrypt a share.          │
└──────────────────────────────────────────────────────────────────────────┘
                                   │  serves secrets to
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  SECURE INTRODUCTION  (how services authenticate — no pre-shared secret)   │
│                                                                            │
│   K8s / RKE2 :  Kubernetes auth method — the pod's ServiceAccount JWT IS   │
│                 the credential. OpenBao validates it against the cluster   │
│                 API. Nothing is pre-distributed. ESO uses this.            │
│   Swarm/bare :  AppRole with **response-wrapped** secret-ids delivered by  │
│                 Ansible at deploy time (single-use, short-TTL wrapping     │
│                 token read from the vault-file root).                      │
└──────────────────────────────────────────────────────────────────────────┘
```

## Why there is no catch-22

| Failure question | Answer |
|---|---|
| "OpenBao is down — can I still operate / recover?" | **Yes.** vault-file + capauth are server-less and hold the encrypted unseal material. Recovery never depends on OpenBao. |
| "Where do the unseal keys live before OpenBao exists?" | They are **created PGP-encrypted** at init (`-pgp-keys`), to capauth operator keys. No plaintext ever hits disk. |
| "How does a brand-new pod read a secret with no credential?" | **K8s auth** — its ServiceAccount JWT is the credential; no secret is pre-shared. |
| "How does a Swarm node get its first secret?" | **Response-wrapped AppRole secret-id** delivered once by Ansible; the wrapping token is single-use + short-TTL. |
| "What is the single irreducible human secret?" | The operator's **capauth PGP private key** (offline) — plus the vault-file password. Everything else derives from these. |

## Order of operations (operator runbook)

0. **Deploy the OpenBao server** (HA Raft):
   - K8s/RKE2: `kubectl apply -f openbao/k8s-helmchart.yaml` (3-replica Raft, TLS from cert-manager).
   - Swarm: `docker stack deploy -c openbao/swarm-compose.yml openbao` (3 nodes, `openbao.hcl`).
   The server comes up **sealed + uninitialized** — that's expected; step 2 inits it.
1. Export operator PGP public keys from capauth: `capauth-ctl export-pgp --out ops/`.
2. `openbao/bootstrap.sh --pgp-keys "ops/op1.asc,ops/op2.asc,..." --root-token-pgp-key ops/op1.asc`
   → produces `openbao-init.enc.json` (PGP-encrypted) in the vault-file root.
3. Unseal: each operator decrypts their share locally (`gpg -d`) and runs
   `bao operator unseal` until threshold is met. No plaintext key leaves the operator's machine.
4. The script enables KV-v2, **Kubernetes auth** (runtime), **AppRole** (nodes), and
   per-env least-privilege policies.
5. Services now resolve secrets via `secrets.factory.get_backend("openbao")` (or
   `vault-file`/`capauth` for the offline lanes) — same `SKSecretBackend` interface either way.

> The bootstrap script refuses to run `bao operator init` without `-pgp-keys` and
> `-root-token-pgp-key`, so it is **impossible** to accidentally generate plaintext
> unseal keys. This invariant is enforced by `tests/test_bootstrap.py`.
