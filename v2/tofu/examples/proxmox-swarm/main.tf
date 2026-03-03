# SKStacks v2 — Example: Proxmox VE + Docker Swarm
#
# On-premises sovereign deployment: provision Proxmox VMs → Ansible Swarm init.
#
# Usage:
#   cd tofu/examples/proxmox-swarm/
#   cp terraform.tfvars.example terraform.tfvars && $EDITOR terraform.tfvars
#   tofu init && tofu plan && tofu apply
#   tofu output -raw ansible_inventory > ../../../platform/docker-swarm/inventory.yml
#   ansible-playbook -i platform/docker-swarm/inventory.yml \
#     platform/docker-swarm/swarm-init.yml

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.51"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.36"
    }
  }
}

module "proxmox_cluster" {
  source = "../../modules/proxmox-cluster"

  proxmox_endpoint        = var.proxmox_endpoint
  proxmox_api_token       = var.proxmox_api_token
  proxmox_skip_tls_verify = var.proxmox_skip_tls_verify
  proxmox_node            = var.proxmox_node

  cluster_name   = var.cluster_name
  env            = var.env
  ssh_public_key = var.ssh_public_key
  vm_template_id = var.vm_template_id
  gateway        = var.gateway

  server_count  = var.manager_count   # Docker Swarm managers
  agent_count   = var.worker_count    # Docker Swarm workers

  server_cores     = var.manager_cores
  server_memory_mb = var.manager_memory_mb
  server_disk_gb   = var.manager_disk_gb
  agent_cores      = var.worker_cores
  agent_memory_mb  = var.worker_memory_mb
  agent_disk_gb    = var.worker_disk_gb
  longhorn_disk_gb = 0                # No Longhorn in Swarm deployments
}

module "cloudflare_dns" {
  source = "../../modules/cloudflare-dns"

  cloudflare_token = var.cloudflare_api_token
  zone_id          = var.cloudflare_zone_id
  cluster_name     = var.cluster_name
  domain           = var.domain
  public_ip        = var.public_ip    # On-premises: your public IP / NAT entry
}

output "ansible_inventory" {
  description = "Paste into platform/docker-swarm/inventory.yml"
  value       = module.proxmox_cluster.ansible_inventory
}

output "cluster_fqdn" {
  value = module.cloudflare_dns.cluster_fqdn
}

output "next_steps" {
  value = <<-EOT

  ✅ Proxmox VMs provisioned!

  Next steps:
  1. Export inventory:
       tofu output -raw ansible_inventory > ../../../platform/docker-swarm/inventory.yml

  2. Init Docker Swarm:
       ansible-playbook -i platform/docker-swarm/inventory.yml \\
         platform/docker-swarm/swarm-init.yml

  3. Deploy core services:
       ansible-playbook -i platform/docker-swarm/inventory.yml \\
         -e env=${var.env} -e secret_backend=${var.secret_backend} \\
         core/skfence/deploy.yml

  EOT
}
