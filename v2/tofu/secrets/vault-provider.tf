# SKStacks v2 — OpenTofu: HashiCorp Vault secret integration
#
# When SKSTACKS_SECRET_BACKEND=hashicorp-vault, add this file to your
# tofu root module to pull cloud provider credentials directly from Vault.
#
# No credentials in .tfvars. No credentials in CI/CD secrets.
# Vault is the single source of truth.
#
# Prerequisites:
#   - Vault running and unsealed
#   - KV-v2 path: kv/data/skstacks/{env}/tofu/{provider}
#   - Vault token / AppRole / OIDC JWT configured (see SECURITY-BACKENDS.md)

terraform {
  required_providers {
    vault = {
      source  = "hashicorp/vault"
      version = "~> 4.4"
    }
  }
}

provider "vault" {
  address   = var.vault_addr
  # Auth: token, AppRole, or K8s — configured via environment variables:
  #   VAULT_TOKEN         → direct token
  #   VAULT_ROLE_ID       → AppRole (combined with VAULT_SECRET_ID)
  #   VAULT_K8S_ROLE      → K8s SA auth (inside cluster)
}

# ── Read cloud provider credentials from Vault ────────────────────────────────

data "vault_kv_secret_v2" "hetzner" {
  count = var.provider_hetzner_enabled ? 1 : 0
  mount = var.vault_kv_mount
  name  = "${var.vault_path_prefix}/${var.env}/tofu/hetzner"
}

data "vault_kv_secret_v2" "proxmox" {
  count = var.provider_proxmox_enabled ? 1 : 0
  mount = var.vault_kv_mount
  name  = "${var.vault_path_prefix}/${var.env}/tofu/proxmox"
}

data "vault_kv_secret_v2" "cloudflare" {
  count = var.provider_cloudflare_enabled ? 1 : 0
  mount = var.vault_kv_mount
  name  = "${var.vault_path_prefix}/${var.env}/tofu/cloudflare"
}

data "vault_kv_secret_v2" "tofu_state" {
  count = var.read_state_credentials ? 1 : 0
  mount = var.vault_kv_mount
  name  = "${var.vault_path_prefix}/${var.env}/tofu/state"
}

# ── Expose as locals for use in provider blocks ───────────────────────────────

locals {
  hetzner_token    = var.provider_hetzner_enabled    ? data.vault_kv_secret_v2.hetzner[0].data["api_token"] : ""
  proxmox_token    = var.provider_proxmox_enabled    ? data.vault_kv_secret_v2.proxmox[0].data["api_token"] : ""
  cloudflare_token = var.provider_cloudflare_enabled ? data.vault_kv_secret_v2.cloudflare[0].data["api_token"] : ""
  cf_zone_id       = var.provider_cloudflare_enabled ? data.vault_kv_secret_v2.cloudflare[0].data["zone_id"] : ""

  state_access_key = var.read_state_credentials ? data.vault_kv_secret_v2.tofu_state[0].data["access_key"] : ""
  state_secret_key = var.read_state_credentials ? data.vault_kv_secret_v2.tofu_state[0].data["secret_key"] : ""
}

# ── Variables ─────────────────────────────────────────────────────────────────

variable "vault_addr" {
  description = "HashiCorp Vault server URL."
  type        = string
  default     = ""
  # Override with VAULT_ADDR env var
}

variable "vault_kv_mount" {
  type    = string
  default = "kv"
}

variable "vault_path_prefix" {
  type    = string
  default = "skstacks"
}

variable "env" {
  type    = string
  default = "prod"
}

variable "provider_hetzner_enabled" {
  type    = bool
  default = false
}

variable "provider_proxmox_enabled" {
  type    = bool
  default = false
}

variable "provider_cloudflare_enabled" {
  type    = bool
  default = false
}

variable "read_state_credentials" {
  description = "Pull S3/MinIO state backend credentials from Vault too."
  type        = bool
  default     = false
}
