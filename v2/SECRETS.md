# SKStacks v2 — Secrets & Authentication Guide

Detailed deployment instructions for every secrets backend, how to wire each
one to every platform, and ready-to-use AI prompts for configuration and
day-2 operations.

See also: `SECURITY-BACKENDS.md` for the comparison matrix and emergency
recovery procedures.

---

## Quick reference

```
SKSTACKS_SECRET_BACKEND=vault-file       → Ansible Vault AES-256, git-native
SKSTACKS_SECRET_BACKEND=hashicorp-vault  → HashiCorp Vault HA Raft, dynamic secrets
SKSTACKS_SECRET_BACKEND=capauth          → Sovereign PGP via skcapstone agent
```

Switch at any time via the migration tool:
```bash
python3 secrets/migrate.py --from vault-file --to hashicorp-vault --env prod --dry-run
```

---

---

# Backend 1 — Ansible Vault (vault-file)

**Best for:** Solo operators, air-gapped environments, existing Ansible
infrastructure, teams that want git-native encrypted secrets with no extra
infra.

**Encryption:** AES-256-GCM via `ansible-vault`.
**Key material:** A password file on disk, optionally stored in OS keychain.
**Offline capable:** Yes — no network required to decrypt.

## How it works

```
secrets/vault_file/examples/vault.example.yml   ← sanitized template
        │
        ▼  ansible-vault encrypt
group_vars/{env}/{scope}-{env}_vault.yml         ← AES-256 blob, safe to commit
        │
        ▼  ansible-vault decrypt (at deploy time)
Ansible vars: vault_{scope}_{key}               ← injected into templates
        │
        ▼  Jinja2 template rendering
docker-compose.yml / K8s manifest               ← ephemeral, never stored
```

## Step-by-step setup

### 1. Create the vault password

```bash
# Generate a strong random password
openssl rand -base64 32 > ~/.vault_pass_env/.prod_vault_pass
chmod 600 ~/.vault_pass_env/.prod_vault_pass

# Optional: store in OS keychain instead of a file
secret-tool store --label='skstacks-prod' service skstacks env prod
# Retrieve: secret-tool lookup service skstacks env prod > ~/.vault_pass_env/.prod_vault_pass
```

**Never commit this file.** Add to `.gitignore`:
```
.vault_pass_env/
*_vault_pass
```

### 2. Create your vault file from the template

```bash
cp secrets/vault_file/examples/vault.example.yml group_vars/prod/vault.yml
```

Edit `group_vars/prod/vault.yml` — replace every `CHANGEME_` value:

```yaml
# ── SKFence (Traefik / ingress) ───────────────────────────────────────────
vault_skfence_cloudflare_dns_token: "your-cf-token-here"
vault_skfence_cloudflare_email: "admin@example.com"
vault_skfence_dashboard_password_hash: "$2y$10$..."  # htpasswd -nB admin

# ── SKSec (CrowdSec) ──────────────────────────────────────────────────────
vault_sksec_bouncer_api_key: "cs-bouncer-key-here"

# ── SKSSO (Authentik) ─────────────────────────────────────────────────────
vault_sksso_secret_key: "$(openssl rand -hex 50)"
vault_sksso_db_password: "strong-db-password"

# ── SKHA (Keepalived) ─────────────────────────────────────────────────────
vault_skha_vrrp_password: "max8chars"          # VRRP auth, max 8 chars

# ── Backup ────────────────────────────────────────────────────────────────
vault_skbackup_encryption_passphrase: "long-random-backup-passphrase"
```

### 3. Encrypt the vault

```bash
ansible-vault encrypt \
  --vault-password-file ~/.vault_pass_env/.prod_vault_pass \
  group_vars/prod/vault.yml
```

Verify it's encrypted (should start with `$ANSIBLE_VAULT;1.1;AES256`):
```bash
head -1 group_vars/prod/vault.yml
```

### 4. Reference vault variables in your main vars

In `group_vars/prod/all.yml`, reference using double-brace syntax:

```yaml
# all.yml — plaintext, safe to commit
cloudflare_dns_token: "{{ vault_skfence_cloudflare_dns_token }}"
crowdsec_bouncer_key: "{{ vault_sksec_bouncer_api_key }}"
authentik_secret_key: "{{ vault_sksso_secret_key }}"
keepalived_auth_pass: "{{ vault_skha_vrrp_password }}"
```

### 5. Use in Ansible playbooks

```bash
# Decrypt inline at deploy time — never written to disk
ansible-playbook -i inventory deploy.yml \
  --vault-password-file ~/.vault_pass_env/.prod_vault_pass

# Or interactive prompt:
ansible-playbook -i inventory deploy.yml --ask-vault-pass
```

### 6. Wire to different platforms

**Docker Swarm:**
```bash
# Render Traefik docker-compose with decrypted secrets
ansible-playbook -i inventory playbooks/deploy.yml \
  --vault-password-file ~/.vault_pass_env/.prod_vault_pass \
  --tags traefik
```

**RKE2 / Kubernetes (via Ansible + kubectl):**
```bash
# Render K8s secret manifests and apply
ansible-playbook -i inventory playbooks/k8s-secrets.yml \
  --vault-password-file ~/.vault_pass_env/.prod_vault_pass
# Creates K8s Secrets from vault vars, then: kubectl apply -f rendered-secrets/
```

**k3d (local dev — use a dev vault):**
```bash
# Separate vault for dev — weaker passwords OK
ansible-vault create \
  --vault-password-file ~/.vault_pass_env/.dev_vault_pass \
  group_vars/dev/vault.yml
```

### 7. Rotate the vault password

```bash
ansible-vault rekey \
  --vault-password-file ~/.vault_pass_env/.prod_vault_pass \
  --new-vault-password-file ~/.vault_pass_env/.prod_vault_pass_new \
  group_vars/prod/vault.yml

mv ~/.vault_pass_env/.prod_vault_pass_new ~/.vault_pass_env/.prod_vault_pass
```

### 8. Edit an existing encrypted vault

```bash
ansible-vault edit \
  --vault-password-file ~/.vault_pass_env/.prod_vault_pass \
  group_vars/prod/vault.yml
# Opens $EDITOR with decrypted content — re-encrypts on save/exit
```

### 9. Back up vault files

Encrypted vault files are **safe to commit to git** — they are opaque without
the password file. The password file itself must be backed up separately:

```bash
# Backup password to a second secure location (e.g. pass, 1Password, Bitwarden)
pass insert skstacks/prod/vault_pass < ~/.vault_pass_env/.prod_vault_pass

# Or to a hardware key (YubiKey via pass + gpg)
gpg --encrypt --recipient YOUR_KEY ~/.vault_pass_env/.prod_vault_pass
```

---

---

# Backend 2 — HashiCorp Vault

**Best for:** Teams, compliance requirements (SOC2, HIPAA, FedRAMP), dynamic
database/PKI credentials, full audit trail, multi-service fine-grained ACL.

**Encryption:** AES-256-GCM in Vault's storage backend.
**Auth methods:** AppRole (CI/CD), Kubernetes JWT (K8s pods), OIDC (humans).
**Dynamic secrets:** Yes — database, PKI, SSH.

## Architecture

```
┌───────────────────────────────────────────────────────────┐
│  HashiCorp Vault HA Raft Cluster (3 nodes)                │
│                                                           │
│  vault-1 (active leader)   vault-2   vault-3             │
│  :8200 API                 :8200     :8200                │
│  /raft/data/               /raft/    /raft/               │
│                                                           │
│  Secret Engines:                                          │
│  kv-v2 at secret/          ← static secrets              │
│  pki/ at pki/              ← internal TLS CA             │
│  database/                 ← dynamic DB credentials       │
│  transit/                  ← encryption-as-a-service      │
│  ssh/                      ← signed SSH certificates      │
│                                                           │
│  Auth Methods:                                            │
│  approle/  ← CI/CD runners, Ansible, Vault Agent         │
│  kubernetes/ ← K8s/RKE2 service account JWTs             │
│  jwt/      ← GitHub Actions OIDC (no static tokens)      │
│  oidc/     ← Human operators via Authentik SSO            │
└───────────────────────────────────────────────────────────┘
```

## KV path convention

```
kv/data/skstacks/{env}/{scope}/{key}

Examples:
  kv/data/skstacks/prod/skfence/cloudflare_dns_token
  kv/data/skstacks/prod/sksec/crowdsec_bouncer_key
  kv/data/skstacks/prod/sksso/authentik_secret_key
  kv/data/skstacks/staging/skfence/cloudflare_dns_token
  kv/data/skstacks/dev/sksso/db_password
```

## Step-by-step deployment — bare metal / Docker

### 1. Install Vault CLI

```bash
# Ubuntu / Debian
wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install vault

# macOS
brew tap hashicorp/tap && brew install hashicorp/tap/vault

# Verify
vault --version   # ≥ 1.15
```

### 2. Run bootstrap script (bare-metal mode)

The scaffold includes a fully idempotent bootstrap script:

```bash
export VAULT_ADDR=https://vault.example.com:8200

# Dry-run first — see every command without executing
bash secrets/hashicorp_vault/bootstrap.sh --dry-run \
  --mode bare \
  --github-org your-github-org \
  --envs dev,staging,prod

# Execute for real
bash secrets/hashicorp_vault/bootstrap.sh \
  --mode bare \
  --github-org your-github-org \
  --envs dev,staging,prod \
  --vault-addr https://vault.example.com:8200
```

The script performs (in order):
1. Detects if Vault is already initialized — skips init if so
2. `vault operator init` — 5 Shamir shares, threshold 3 → saves to `vault-init.json`
3. Unseal all 3 Raft nodes with 3 of the 5 keys
4. Waits for Raft leader election
5. Enables file audit log
6. Enables KV-v2 at `secret/`
7. Enables JWT auth (GitHub OIDC — no static tokens in CI)
8. Writes per-env read/write policies
9. Enables AppRole; creates per-env deploy roles
10. Prints final status summary

**Immediately after bootstrap — secure the init output:**
```bash
# vault-init.json contains unseal keys + root token — protect it
gpg --encrypt --recipient YOUR_KEY vault-init.json
mv vault-init.json vault-init.json.gpg
# Store unseal keys in separate secure locations (never all in one place)
```

### 3. Run bootstrap script — Kubernetes / RKE2 mode

```bash
bash secrets/hashicorp_vault/bootstrap.sh \
  --mode k8s \
  --namespace vault \
  --github-org your-github-org \
  --envs dev,staging,prod
```

In K8s mode the script uses `kubectl exec` into the vault pod to run
`vault operator` commands, so no direct network access to Vault is needed.

### 4. Deploy Vault on RKE2 / Kubernetes via Helm

```bash
helm repo add hashicorp https://helm.releases.hashicorp.com
helm repo update

helm install vault hashicorp/vault \
  --namespace vault \
  --create-namespace \
  --values secrets/hashicorp_vault/helm/vault-values.yaml
```

The included `vault-values.yaml` configures:
- HA mode with 3 replicas
- Integrated Raft storage (no external etcd/consul)
- TLS auto-cert from cert-manager
- Audit logging to stdout (captured by K8s log aggregation)

Monitor initialization:
```bash
kubectl get pods -n vault --watch
# vault-0, vault-1, vault-2 should go Running → then one becomes active
```

### 5. Store secrets

```bash
export VAULT_ADDR=https://vault.example.com:8200
export VAULT_TOKEN=$(cat vault-init.json | jq -r .root_token)

# Store individual secrets
vault kv put kv/data/skstacks/prod/skfence \
  cloudflare_dns_token="your-cf-token" \
  dashboard_password_hash='$2y$10$...'

# Store all at once from an env file
vault kv put kv/data/skstacks/prod/sksso \
  secret_key="$(openssl rand -hex 50)" \
  db_password="$(openssl rand -base64 24)" \
  redis_password="$(openssl rand -base64 24)"
```

### 6. Apply policies

```bash
# Deploy role — read-only across all scopes (for CI/CD runners)
vault policy write skstacks-deploy \
  secrets/hashicorp_vault/policies/skstacks-policy.hcl

# RKE2 / ESO role — read access via K8s service account JWT
vault policy write rke2-node \
  secrets/hashicorp_vault/policies/rke2-node-policy.hcl
```

### 7. Configure AppRole (for Ansible and CI/CD)

```bash
vault auth enable approle

# Create a deploy role
vault write auth/approle/role/skstacks-deploy \
  token_policies="skstacks-deploy" \
  token_ttl=1h \
  token_max_ttl=4h \
  secret_id_ttl=24h

# Get the role ID (safe to store in CI as non-secret)
vault read auth/approle/role/skstacks-deploy/role-id

# Get a secret ID (rotate every 24h in CI)
vault write -f auth/approle/role/skstacks-deploy/secret-id
```

### 8. Configure Kubernetes auth (for ESO on RKE2)

```bash
vault auth enable kubernetes

vault write auth/kubernetes/config \
  kubernetes_host="https://$(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}')" \
  token_reviewer_jwt="$(kubectl create token vault-auth -n vault)" \
  kubernetes_ca_cert=@<(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d)

# Bind ESO service account to the rke2-node policy
vault write auth/kubernetes/role/skstacks-eso \
  bound_service_account_names=external-secrets \
  bound_service_account_namespaces=external-secrets \
  token_policies="rke2-node" \
  token_ttl=10m
```

### 9. Configure GitHub Actions JWT auth (no static tokens)

```bash
vault write auth/jwt/config \
  oidc_discovery_url="https://token.actions.githubusercontent.com" \
  bound_issuer="https://token.actions.githubusercontent.com"

# Allow the repo's Actions runners to read staging secrets
vault write auth/jwt/role/github-staging-read \
  role_type="jwt" \
  user_claim="actor" \
  bound_claims='{"repository":"your-org/skstacks"}' \
  token_policies="skstacks-deploy" \
  token_ttl=15m
```

In your GitHub Actions workflow:
```yaml
- name: Get Vault secrets
  uses: hashicorp/vault-action@v3
  with:
    url: https://vault.example.com:8200
    method: jwt
    role: github-staging-read
    secrets: |
      kv/data/skstacks/staging/skfence cloudflare_dns_token | CF_DNS_TOKEN
```

### 10. Deploy Vault Agent sidecar (Docker Swarm / bare metal)

For services running outside Kubernetes, use Vault Agent to render secrets
to tmpfs files that services read as env files:

```bash
# vault-agent.hcl is scaffolded at secrets/hashicorp_vault/vault-agent.hcl
# Edit: set vault.address to your Vault address
# Edit: add template blocks for each service scope

vault agent -config=secrets/hashicorp_vault/vault-agent.hcl
```

The agent authenticates via AppRole and renders templates like:
```
{{ with secret "kv/data/skstacks/prod/skfence" }}
CLOUDFLARE_DNS_TOKEN={{ .Data.data.cloudflare_dns_token }}
{{ end }}
```
to `/run/secrets/skfence.env` (tmpfs — never touches disk).

### 11. Verify

```bash
vault status                            # Initialized: true, Sealed: false
vault audit list                        # file/ enabled
vault secrets list                      # kv-v2 at secret/
vault auth list                         # approle/, kubernetes/, jwt/
vault kv get kv/data/skstacks/prod/skfence
vault policy list                       # skstacks-deploy, rke2-node
```

---

---

# Backend 3 — CapAuth (Sovereign PGP)

**Best for:** Maximum sovereignty, sovereign agent mesh, offline/air-gap,
PGP-signed secret provenance, skcapstone integration, full self-custody.

**Encryption:** PGP (GnuPG) — RSA-4096 or Ed25519+Curve25519.
**Key material:** GnuPG keyring, backed up in skcapstone soul blueprint.
**Offline capable:** Yes — GnuPG fallback mode requires no network.

## How it works

```
capauth.example.yaml  ← configure recipients per environment
       │
       ▼
PGP-encrypted JSON blobs:
~/.skstacks/secrets/{env}/{scope}.gpg

Two decryption modes:
  Mode 1 — skcapstone MCP (default):
    Deploy tooling → HTTP POST to skcapstone MCP server
    → agent decrypts with private key → returns plaintext
    → private key NEVER leaves agent process

  Mode 2 — Direct GnuPG (fallback):
    Deploy tooling → gpg --decrypt {blob}
    → reads from local GnuPG keyring
    → automatic fallback if skcapstone offline
```

## Step-by-step setup

### 1. Generate or locate your PGP key

```bash
# Check existing keys
gpg --list-keys --keyid-format LONG

# Generate a new key (Ed25519 recommended for new deployments)
gpg --full-generate-key
# → Choose: (9) ECC and ECC
# → Choose: (1) Curve 25519
# → Set expiry: 2y (rotate annually)
# → Enter name and email

# Get your fingerprint (40-char hex)
gpg --list-keys --with-fingerprint --keyid-format LONG | grep fingerprint
# Example: Key fingerprint = 6136 E987 BC79 5A25 E06B  BBE1 985F ADA5 1534 3091
```

### 2. Get skcapstone agent fingerprint

If using the skcapstone agent as a decryptor (recommended):

```bash
skcapstone soul_show | grep fingerprint
# Or check CLAUDE.md: Fingerprint: 6136E987BC795A25E06BBBE1985FADA515343091
```

### 3. Configure capauth.yaml

```bash
cp secrets/capauth/capauth.example.yaml ~/.skstacks/capauth.yaml
```

Edit `~/.skstacks/capauth.yaml`:

```yaml
default:
  recipients:
    - fingerprint: "YOUR_40_CHAR_FINGERPRINT_HERE"
      label: "primary-operator"

prod:
  recipients:
    - fingerprint: "YOUR_PROD_OPERATOR_FINGERPRINT"
      label: "prod-operator"
    - fingerprint: "6136E987BC795A25E06BBBE1985FADA515343091"
      label: "opus-agent"

staging:
  recipients:
    - fingerprint: "YOUR_FINGERPRINT"
      label: "operator"
    - fingerprint: "6136E987BC795A25E06BBBE1985FADA515343091"
      label: "opus-agent"

dev:
  recipients:
    - fingerprint: "YOUR_FINGERPRINT"
      label: "operator"
```

### 4. Create the secret store directory

```bash
mkdir -p ~/.skstacks/secrets/{prod,staging,dev}
chmod 700 ~/.skstacks/secrets
```

### 5. Store secrets

**Via Python backend:**
```python
from secrets.factory import get_backend
import os
os.environ["SKSTACKS_SECRET_BACKEND"] = "capauth"

backend = get_backend()
backend.set("skfence", "prod", "cloudflare_dns_token", "your-cf-token")
backend.set("sksso",   "prod", "db_password",          "strong-password")
```

**Via skcapstone MCP (agent stores as tagged memory + PGP blob):**
```bash
# Using the MCP tool directly
skcapstone mcp call skstacks_secret_set \
  --key cloudflare_dns_token \
  --value "your-cf-token" \
  --scope skfence \
  --env prod \
  --backend capauth
```

**Manual (direct GnuPG):**
```bash
# Create JSON payload
echo '{"cloudflare_dns_token": "your-cf-token", "dashboard_hash": "$2y$..."}' \
  | gpg --encrypt \
        --armor \
        --recipient YOUR_FINGERPRINT \
        --recipient OPUS_AGENT_FINGERPRINT \
        --output ~/.skstacks/secrets/prod/skfence.gpg
```

### 6. Retrieve secrets

**Via Python backend:**
```python
token = backend.get("skfence", "prod", "cloudflare_dns_token")
```

**Via skcapstone MCP:**
```bash
skcapstone mcp call skstacks_secret_get \
  --key cloudflare_dns_token \
  --scope skfence \
  --env prod
```

**Direct GnuPG:**
```bash
gpg --decrypt ~/.skstacks/secrets/prod/skfence.gpg
# Returns JSON: {"cloudflare_dns_token": "your-cf-token", ...}
```

### 7. Configure the backend for deploy tooling

```bash
export SKSTACKS_SECRET_BACKEND=capauth
export CAPAUTH_MODE=mcp                              # or: gnupg
export CAPAUTH_KEY_ID=6136E987BC795A25E06BBBE1985FADA515343091
export SKCAPSTONE_PORT=9475                          # skcapstone daemon port
```

### 8. Ensure skcapstone MCP server is running

```bash
# Check daemon status
skcapstone status

# Start if not running
skcapstone daemon start

# Verify MCP endpoint
curl -s http://localhost:9475/health | python3 -m json.tool
```

### 9. Wire to Ansible (capauth → Ansible vars)

```python
# In an Ansible lookup plugin or pre-task Python script:
import os, json, httpx

def get_capauth_secret(scope, env, key):
    r = httpx.post(f"http://localhost:{os.getenv('SKCAPSTONE_PORT','9475')}/mcp",
                   json={"tool": "skstacks_secret_get",
                         "args": {"scope": scope, "env": env, "key": key}})
    return r.json()["result"]["value"]

# Or call from Ansible task:
# - name: Get CF token
#   ansible.builtin.set_fact:
#     cf_token: "{{ lookup('pipe', 'python3 get_secret.py skfence prod cloudflare_dns_token') }}"
```

### 10. Wire to Kubernetes / RKE2 via capauth-eso-provider

The capauth ESO provider sidecar runs on each RKE2 node and communicates
with the node-local skcapstone agent via Unix socket:

```bash
# Deploy ESO provider (add to RKE2 auto-deploy manifests):
kubectl apply -f platform/kubernetes/external-secrets/cluster-secret-store.yaml

# The ClusterSecretStore for capauth:
# apiVersion: external-secrets.io/v1beta1
# kind: ClusterSecretStore
# spec:
#   provider:
#     webhook:
#       url: "http://capauth-eso-provider.capauth.svc:8080/secret"
#       headers:
#         Authorization: "Bearer {{ .auth.secret.token }}"
```

### 11. Add a new operator (multi-agent setup)

When adding a second operator or agent who needs to decrypt existing secrets:

```bash
# 1. Get their fingerprint
NEW_FINGERPRINT="AABBCCDD..."

# 2. Add to capauth.yaml recipients for each env
# 3. Re-encrypt all blobs to include the new recipient:
python3 secrets/capauth/reencrypt.py --env prod --dry-run
python3 secrets/capauth/reencrypt.py --env prod

# 4. Verify new operator can decrypt:
gpg --decrypt ~/.skstacks/secrets/prod/skfence.gpg
```

### 12. Rotate PGP keys

```bash
# Generate new key
gpg --full-generate-key
NEW_FINGERPRINT=$(gpg --list-keys --with-colons | grep ^fpr | tail -1 | cut -d: -f10)

# Update capauth.yaml — add new fingerprint, keep old temporarily
# Re-encrypt all blobs to new key
python3 secrets/capauth/reencrypt.py --all-envs --new-fingerprint $NEW_FINGERPRINT

# Once verified — remove old key from capauth.yaml and re-encrypt again
# Then revoke the old key:
gpg --gen-revoke OLD_FINGERPRINT > revoke.asc
gpg --import revoke.asc
```

### 13. Sync blobs across agents (Syncthing)

CapAuth blobs sync automatically between agents via SKComm Syncthing:

```bash
# Check sync status
skcapstone status | grep -A2 Sync

# Manual push (sends encrypted blobs to all peers)
skcapstone sync_push

# Verify a peer received your blobs
skcapstone memory search "skstacks secret" --tags secret,prod
```

---

---

# Choosing Between Backends

## Decision flowchart

```
Do you need dynamic DB credentials or PKI certs?
├── Yes → hashicorp-vault
└── No  →
         Do you need a full audit log for compliance?
         ├── Yes → hashicorp-vault
         └── No  →
                  Do you want maximum sovereignty / air-gap?
                  ├── Yes → capauth
                  └── No  →
                           Do you have existing Ansible infrastructure?
                           ├── Yes → vault-file
                           └── No  → vault-file (simplest start)

Upgrade path: vault-file → hashicorp-vault → capauth (all migratable)
```

## Per-platform recommendations

| Platform | Recommended backend | Rationale |
|----------|---------------------|-----------|
| Docker Swarm (prod) | `hashicorp-vault` | Vault Agent sidecar, no secrets in compose files |
| Docker Swarm (solo) | `vault-file` | Simple, no extra infra |
| RKE2 | `hashicorp-vault` | ESO native integration, dynamic DB creds |
| Vanilla K8s | `hashicorp-vault` | Same as RKE2 |
| k3d (local dev) | `vault-file` | Fast, no server needed |
| k3d (CI) | `vault-file` or env vars | Ephemeral, minimal complexity |
| Sovereign mesh | `capauth` | skcapstone-native, offline capable |

---

---

# AI Configuration Prompts

## Ansible Vault (vault-file)

### Prompt A: Full interactive setup

```
Set up Ansible Vault secrets for a SKStacks v2 deployment.
Working directory: skstacks/v2/

Ask me:
1. Which environment? (prod / staging / dev)
2. Which service scopes do you need? (skfence, sksec, sksso, skbackup, skha — or custom)
3. Do you have a vault password already, or should I generate one?

Then:
1. Generate a random vault password if needed: openssl rand -base64 32
2. Save to ~/.vault_pass_env/.{env}_vault_pass with chmod 600
3. Add vault_pass_env/ to .gitignore
4. Copy secrets/vault_file/examples/vault.example.yml to group_vars/{env}/vault.yml
5. Ask me for each CHANGEME_ value one at a time and substitute them
6. Encrypt: ansible-vault encrypt --vault-password-file ~/.vault_pass_env/.{env}_vault_pass group_vars/{env}/vault.yml
7. Show me the first line to verify encryption
8. Show me how to reference each var in group_vars/{env}/all.yml
```

### Prompt B: Rotate a specific secret

```
Rotate a specific secret in an Ansible Vault.
Working directory: skstacks/v2/

Ask me:
1. Environment (prod/staging/dev)
2. Which secret scope and key to rotate (e.g. skfence/cloudflare_dns_token)
3. New value (or should I generate one?)

Then:
1. Decrypt the vault: ansible-vault decrypt --vault-password-file ~/.vault_pass_env/.{env}_vault_pass group_vars/{env}/vault.yml
2. Update the specific key with the new value
3. Re-encrypt: ansible-vault encrypt --vault-password-file ...
4. Verify: ansible-vault view --vault-password-file ... group_vars/{env}/vault.yml | grep {key}
5. Remind me to re-deploy the affected service
```

### Prompt C: Migrate vault-file to hashicorp-vault

```
Migrate all secrets from Ansible Vault to HashiCorp Vault.
Working directory: skstacks/v2/

Ask me:
1. Source environment (prod/staging/dev)
2. Vault password file path
3. HashiCorp Vault address
4. HashiCorp Vault token (or AppRole credentials)

Then:
1. Dry-run the migration: python3 secrets/migrate.py --from vault-file --to hashicorp-vault --env {env} --dry-run
2. Show me the list of secrets that will be migrated
3. Ask for confirmation
4. Execute: python3 secrets/migrate.py --from vault-file --to hashicorp-vault --env {env}
5. Verify each key exists in Vault: vault kv get kv/data/skstacks/{env}/{scope}
6. Update .env: SKSTACKS_SECRET_BACKEND=hashicorp-vault
```

---

## HashiCorp Vault

### Prompt A: Full bootstrap — bare metal

```
Bootstrap a HashiCorp Vault HA cluster on bare metal using SKStacks v2.
Working directory: skstacks/v2/

Ask me:
1. Vault cluster addresses (3 nodes, e.g. https://10.0.1.10:8200)
2. GitHub org name for OIDC JWT auth
3. Environments to configure (dev, staging, prod)
4. Domain name for Vault UI (e.g. vault.example.com)

Then:
1. Check prerequisites: vault CLI installed, jq installed, VAULT_ADDR reachable
2. Run dry-run first:
   bash secrets/hashicorp_vault/bootstrap.sh --dry-run --mode bare \
     --github-org {org} --envs {envs} --vault-addr {addr}
3. Show me the full command list for review
4. Ask for confirmation, then run without --dry-run
5. Save vault-init.json (contains unseal keys + root token)
6. Encrypt vault-init.json immediately:
   gpg --encrypt --recipient {my_key} vault-init.json
7. Apply policies: vault policy write skstacks-deploy secrets/hashicorp_vault/policies/skstacks-policy.hcl
8. Create AppRole and output role-id and secret-id
9. Run verification: vault status && vault secrets list && vault auth list
Print a summary of what was configured.
```

### Prompt B: Bootstrap on RKE2 / Kubernetes

```
Deploy HashiCorp Vault on an existing RKE2/Kubernetes cluster via Helm.
Working directory: skstacks/v2/

Prerequisites I need you to verify:
- kubectl connected to the cluster (kubectl cluster-info)
- Helm installed (helm version)
- cert-manager running (kubectl get pods -n cert-manager)

Steps:
1. helm repo add hashicorp https://helm.releases.hashicorp.com && helm repo update
2. helm install vault hashicorp/vault --namespace vault --create-namespace \
     --values secrets/hashicorp_vault/helm/vault-values.yaml
3. Wait for pods: kubectl get pods -n vault --watch (until 3/3 Running)
4. Run bootstrap in K8s mode:
   bash secrets/hashicorp_vault/bootstrap.sh --mode k8s --namespace vault \
     --github-org {ask me} --envs dev,staging,prod
5. Configure K8s auth for ESO:
   vault auth enable kubernetes
   vault write auth/kubernetes/config kubernetes_host=https://$(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}')
   vault write auth/kubernetes/role/skstacks-eso bound_service_account_names=external-secrets bound_service_account_namespaces=external-secrets token_policies=rke2-node token_ttl=10m
6. Apply RKE2 node policy: vault policy write rke2-node secrets/hashicorp_vault/policies/rke2-node-policy.hcl
7. Apply ESO ClusterSecretStore: kubectl apply -f platform/kubernetes/external-secrets/cluster-secret-store.yaml
8. Verify: kubectl get clustersecretstore
```

### Prompt C: Store and verify all service secrets

```
Populate HashiCorp Vault with all SKStacks service secrets interactively.
Working directory: skstacks/v2/

Ask me for each of the following secrets one at a time
(offer to generate random values where appropriate):

skfence scope (Traefik / ingress):
- cloudflare_dns_token (Zone.DNS:Edit token from Cloudflare dashboard)
- cloudflare_email
- dashboard_password_hash (offer to generate via: htpasswd -nB admin)

sksec scope (CrowdSec):
- bouncer_api_key
- crowdsec_enrollment_key

sksso scope (Authentik SSO):
- secret_key (offer to generate: openssl rand -hex 50)
- db_password (offer to generate: openssl rand -base64 24)
- redis_password (offer to generate)
- bootstrap_email
- bootstrap_password

skbackup scope (Duplicati / backup):
- encryption_passphrase (offer to generate)
- s3_access_key (or leave empty if no S3)
- s3_secret_key

skha scope (Keepalived):
- vrrp_password (max 8 chars — offer to generate)

Then for each scope:
  vault kv put kv/data/skstacks/{env}/{scope} {key1}={val1} {key2}={val2} ...

Verify: vault kv get kv/data/skstacks/{env}/skfence
```

### Prompt D: Day-2 operations — rotate AppRole secret_id

```
Rotate the Vault AppRole secret_id for CI/CD pipelines.
This should run monthly or after any suspected credential exposure.

Steps:
1. vault write -force auth/approle/role/skstacks-deploy/secret-id
   (generates a new secret_id, invalidates the old one)
2. Update the secret_id in all CI/CD systems:
   - GitHub Actions: gh secret set VAULT_SECRET_ID --body {new_secret_id}
   - Forgejo: Ask me for the Forgejo API URL and token to update via API
3. Verify the new credentials work:
   vault write auth/approle/login \
     role_id=$(vault read -field=role_id auth/approle/role/skstacks-deploy/role-id) \
     secret_id={new_secret_id}
4. Store new secret_id in skcapstone memory:
   skcapstone memory store "Vault AppRole secret_id rotated on $(date +%Y-%m-%d)" --tags vault,rotation,security
```

---

## CapAuth (Sovereign PGP)

### Prompt A: Full initial setup

```
Set up CapAuth sovereign PGP secrets for SKStacks v2.
Working directory: skstacks/v2/

Ask me:
1. Do you have an existing GPG key? (yes → get fingerprint; no → generate one)
2. Is the skcapstone agent (Opus) running? (yes → get its fingerprint from: skcapstone soul_show)
3. Which environments? (prod / staging / dev)

Then:
1. If no GPG key: gpg --full-generate-key (guide through ECC Ed25519, 2y expiry)
2. Get fingerprint: gpg --list-keys --with-fingerprint --keyid-format LONG
3. Get Opus fingerprint: skcapstone soul_show | grep fingerprint
4. Copy secrets/capauth/capauth.example.yaml to ~/.skstacks/capauth.yaml
5. Fill in fingerprints for each environment in capauth.yaml
6. Create secret store dirs: mkdir -p ~/.skstacks/secrets/{prod,staging,dev}
7. Set environment: export SKSTACKS_SECRET_BACKEND=capauth
8. Test: python3 -c "from secrets.factory import get_backend; b = get_backend(); print(b.health_check())"
9. Walk me through storing the first secret interactively
Print the capauth.yaml content for review before saving.
```

### Prompt B: Store all service secrets via skcapstone MCP

```
Store all SKStacks service secrets using the CapAuth backend via skcapstone MCP.

Prerequisites:
- skcapstone daemon running (skcapstone status)
- capauth.yaml configured (cat ~/.skstacks/capauth.yaml)
- SKSTACKS_SECRET_BACKEND=capauth

Ask me for each secret below. Offer to generate random values where marked [gen].

skfence:
- cloudflare_dns_token
- dashboard_password_hash [gen: htpasswd -nB admin]

sksso:
- secret_key [gen: openssl rand -hex 50]
- db_password [gen: openssl rand -base64 24]

sksec:
- bouncer_api_key

skbackup:
- encryption_passphrase [gen: openssl rand -base64 32]

skha:
- vrrp_password [gen: 8 chars]

For each secret, call:
  skcapstone mcp call skstacks_secret_set \
    --scope {scope} --env prod --key {key} --value {value} --backend capauth

Then verify each was stored:
  skcapstone mcp call skstacks_secret_get --scope {scope} --env prod --key {key}

Print a summary table of all stored scopes and key names (not values).
```

### Prompt C: Add a new authorized agent/operator

```
Add a new PGP key as an authorized decryptor for SKStacks CapAuth secrets.
Working directory: skstacks/v2/

Ask me:
1. New operator/agent PGP fingerprint (40-char hex)
2. Label for this key (e.g. "lumina-agent", "dev-operator-alice")
3. Which environments should they access? (prod/staging/dev/all)

Then:
1. Add fingerprint to ~/.skstacks/capauth.yaml under specified environments
2. Show me the updated capauth.yaml for review
3. Dry-run re-encryption for each affected env:
   python3 secrets/capauth/reencrypt.py --env {env} --dry-run
4. Show list of blobs that will be re-encrypted
5. Ask for confirmation, then execute:
   python3 secrets/capauth/reencrypt.py --env {env}
6. Verify new operator can decrypt:
   gpg --decrypt ~/.skstacks/secrets/{env}/skfence.gpg
7. Store audit memory:
   skcapstone memory store "CapAuth: added {label} ({fingerprint[:8]}) to {envs} on $(date +%Y-%m-%d)" \
     --tags capauth,key-management,security --importance 0.8
```

### Prompt D: Emergency recovery — skcapstone offline

```
The skcapstone MCP server is offline and I need to decrypt CapAuth secrets
for an emergency deployment.

CapAuth automatically falls back to direct GnuPG mode. Steps:
1. Check skcapstone daemon: skcapstone status (expect: offline or error)
2. Set fallback mode: export CAPAUTH_MODE=gnupg
3. Verify your GPG key is in keyring: gpg --list-secret-keys
4. Test decrypt: gpg --decrypt ~/.skstacks/secrets/prod/skfence.gpg
5. If key not in keyring, restore from backup:
   gpg --import backup-key.asc
6. Run deployment in gnupg fallback mode:
   CAPAUTH_MODE=gnupg python3 deploy.py --env prod
7. After recovery, restart skcapstone: skcapstone daemon start
8. Verify MCP mode works again: CAPAUTH_MODE=mcp python3 -c "from secrets.factory import get_backend; print(get_backend().health_check())"
9. Store incident memory:
   skcapstone memory store "Emergency deploy: skcapstone offline, used GnuPG fallback on $(date)" \
     --tags incident,capauth,recovery --importance 0.9
```

---

## Cross-backend prompts

### Prompt: Full health check across all backends

```
Run a health check across all SKStacks v2 secret backends.
Working directory: skstacks/v2/

Steps:
1. Run the factory health report:
   python3 -c "from secrets.factory import health_report; import json; print(json.dumps(health_report(), indent=2))"

2. For vault-file:
   - Check vault files exist: ls group_vars/*/vault.yml
   - Verify encryption: head -1 group_vars/prod/vault.yml (should start with $ANSIBLE_VAULT)
   - Test decrypt: ansible-vault view --vault-password-file ~/.vault_pass_env/.prod_vault_pass group_vars/prod/vault.yml > /dev/null && echo OK

3. For hashicorp-vault (if configured):
   - Check: vault status
   - Check seal status: vault status | grep Sealed
   - Check all expected paths: vault kv list kv/data/skstacks/prod/

4. For capauth (if configured):
   - Check skcapstone: skcapstone status | grep -A2 identity
   - Check blobs exist: ls ~/.skstacks/secrets/prod/
   - Test decrypt one blob: python3 -c "from secrets.factory import get_backend; b=get_backend('capauth'); print(b.health_check())"

Report: overall status, which backends are healthy, any missing secrets or sealed vaults.
```

### Prompt: Complete secrets audit

```
Audit all secrets across all configured SKStacks v2 backends.
Working directory: skstacks/v2/

For each environment (dev, staging, prod):
1. List all scopes and key names (not values) from the active backend
2. Check for: missing expected keys, keys expiring within 30 days (Vault leases)
3. Check last rotation date for each key (from Vault metadata or git log)
4. Flag any keys that haven't been rotated in > 180 days
5. Check that capauth.yaml recipients are all still valid (no revoked keys)
6. Verify Vault AppRole secret_id was rotated within 30 days:
   vault read auth/approle/role/skstacks-deploy/role-id

Output a markdown table:
| Scope | Key | Last Rotated | Expires | Status |
|-------|-----|--------------|---------|--------|
...

Flag anything RED (expired), YELLOW (due for rotation), GREEN (current).
Store audit result in skcapstone memory with tag=secrets-audit.
```
