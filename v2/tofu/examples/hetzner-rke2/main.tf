# SKStacks v2 — Example: Hetzner Cloud + RKE2
#
# Complete end-to-end: provision Hetzner VMs → DNS → Ansible RKE2 install.
#
# Usage:
#   cd tofu/examples/hetzner-rke2/
#   cp terraform.tfvars.example terraform.tfvars
#   $EDITOR terraform.tfvars
#
#   # Initialize with S3/MinIO state
#   tofu init \
#     -backend-config="bucket=skstacks-tofu-state" \
#     -backend-config="key=prod/skstack01/terraform.tfstate" \
#     -backend-config="endpoint=https://minio.your-domain.com" \
#     -backend-config="access_key=$MINIO_ACCESS_KEY" \
#     -backend-config="secret_key=$MINIO_SECRET_KEY"
#
#   tofu plan
#   tofu apply
#
#   # Export Ansible inventory and run RKE2 install
#   tofu output -raw ansible_inventory > ../../../platform/rke2/ansible/inventory.yml
#   cd ../../../
#   ansible-playbook -i platform/rke2/ansible/inventory.yml \
#     platform/rke2/ansible/install-rke2-server.yml
#   ansible-playbook -i platform/rke2/ansible/inventory.yml \
#     platform/rke2/ansible/install-rke2-agent.yml

terraform {
  required_version = ">= 1.6.0"

  # State backend — copy s3-backend.tf from state/ and configure
  # backend "s3" { ... }

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.47"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.36"
    }
  }
}

# ── Locals ────────────────────────────────────────────────────────────────────

locals {
  # Secret resolution: reads from vault-file, hashicorp-vault, or capauth
  # depending on SKSTACKS_SECRET_BACKEND environment variable.
  # For vault-file: run `eval $(../../secrets/vault-file-wrapper.sh --env ${var.env})` first.
  # For hashicorp-vault: see ../../secrets/vault-provider.tf

  hcloud_token     = var.hcloud_token
  cf_api_token     = var.cloudflare_api_token
  cf_zone_id       = var.cloudflare_zone_id
}

# ── Compute: Hetzner cluster ──────────────────────────────────────────────────

module "hetzner_cluster" {
  source = "../../modules/hetzner-cluster"

  hcloud_token   = local.hcloud_token
  cluster_name   = var.cluster_name
  env            = var.env
  ssh_public_key = var.ssh_public_key

  server_count     = var.server_count
  server_type      = var.server_type
  agent_count      = var.agent_count
  agent_server_type = var.agent_server_type
  image            = var.image
  location         = var.location

  ssh_allowed_cidrs   = var.ssh_allowed_cidrs
  create_floating_ip  = true
  enable_turn_ports   = var.deploy_coturn
}

# ── DNS: Cloudflare records ───────────────────────────────────────────────────

module "cloudflare_dns" {
  source = "../../modules/cloudflare-dns"

  cloudflare_token = local.cf_api_token
  zone_id          = local.cf_zone_id
  cluster_name     = var.cluster_name
  domain           = var.domain
  public_ip        = module.hetzner_cluster.vip_address

  create_turn_record = var.deploy_coturn
  enable_waf_rules   = var.enable_cloudflare_waf
}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "vip_address" {
  description = "Cluster VIP (kube-apiserver + ingress entry point)."
  value       = module.hetzner_cluster.vip_address
}

output "server_public_ips" {
  value = module.hetzner_cluster.server_public_ips
}

output "agent_public_ips" {
  value = module.hetzner_cluster.agent_public_ips
}

output "cluster_fqdn" {
  value = module.cloudflare_dns.cluster_fqdn
}

output "wildcard_fqdn" {
  value = module.cloudflare_dns.wildcard_fqdn
}

output "ansible_inventory" {
  description = "Paste into platform/rke2/ansible/inventory.yml"
  value       = module.hetzner_cluster.ansible_inventory
  sensitive   = false
}

output "next_steps" {
  value = <<-EOT

  ✅ Infrastructure provisioned!

  VIP:     ${module.hetzner_cluster.vip_address}
  Cluster: ${module.cloudflare_dns.cluster_fqdn}

  Next steps:
  1. Export Ansible inventory:
       tofu output -raw ansible_inventory > ../../../platform/rke2/ansible/inventory.yml

  2. Install RKE2 server nodes:
       ansible-playbook -i platform/rke2/ansible/inventory.yml \\
         platform/rke2/ansible/install-rke2-server.yml

  3. Install RKE2 agent nodes:
       ansible-playbook -i platform/rke2/ansible/inventory.yml \\
         platform/rke2/ansible/install-rke2-agent.yml

  4. Bootstrap ArgoCD:
       kubectl apply -f cicd/argocd/app-of-apps.yaml

  EOT
}
