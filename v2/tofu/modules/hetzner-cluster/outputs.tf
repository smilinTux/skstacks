# Outputs consumed by the Ansible dynamic inventory generator
# and by other tofu modules (e.g. cloudflare-dns).

output "server_public_ips" {
  description = "Public IPv4 addresses of server (control-plane) nodes."
  value       = hcloud_server.servers[*].ipv4_address
}

output "server_private_ips" {
  description = "Private IPv4 addresses of server nodes."
  value       = [for s in hcloud_server.servers : s.network[*].ip[0]]
}

output "agent_public_ips" {
  description = "Public IPv4 addresses of agent (worker) nodes."
  value       = hcloud_server.agents[*].ipv4_address
}

output "agent_private_ips" {
  description = "Private IPv4 addresses of agent nodes."
  value       = [for a in hcloud_server.agents : a.network[*].ip[0]]
}

output "vip_address" {
  description = "Floating IP address (kube-apiserver VIP)."
  value       = var.create_floating_ip ? hcloud_floating_ip.vip[0].ip_address : hcloud_server.servers[0].ipv4_address
}

output "private_network_id" {
  description = "Hetzner private network ID."
  value       = hcloud_network.private.id
}

output "ssh_key_id" {
  description = "Hetzner SSH key ID."
  value       = hcloud_ssh_key.deploy.id
}

# ── Ansible inventory fragment ────────────────────────────────────────────────
# Write this output to a file for use by Ansible:
#   tofu output -raw ansible_inventory > platform/rke2/ansible/inventory.yml

output "ansible_inventory" {
  description = "YAML inventory for use with Ansible RKE2 playbooks."
  sensitive   = false
  value = yamlencode({
    all = {
      vars = {
        ansible_user                = "ubuntu"
        ansible_ssh_private_key_file = "~/.ssh/skstacks_deploy"
        rke2_vip_ip                 = var.create_floating_ip ? hcloud_floating_ip.vip[0].ip_address : hcloud_server.servers[0].ipv4_address
        cluster_name                = var.cluster_name
      }
      children = {
        rke2_servers = {
          hosts = {
            for i, s in hcloud_server.servers :
            s.name => {
              ansible_host = s.ipv4_address
              rke2_role    = "server"
              private_ip   = s.network[*].ip[0]
            }
          }
        }
        rke2_agents = {
          hosts = {
            for i, a in hcloud_server.agents :
            a.name => {
              ansible_host = a.ipv4_address
              rke2_role    = "agent"
              private_ip   = a.network[*].ip[0]
            }
          }
        }
      }
    }
  })
}
