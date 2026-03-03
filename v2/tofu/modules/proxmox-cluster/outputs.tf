output "server_ips" {
  description = "IP addresses of server (control-plane) VMs."
  value       = [for vm in proxmox_virtual_environment_vm.servers : vm.initialization[0].ip_config[0].ipv4[0].address]
}

output "agent_ips" {
  description = "IP addresses of agent (worker) VMs."
  value       = [for vm in proxmox_virtual_environment_vm.agents : vm.initialization[0].ip_config[0].ipv4[0].address]
}

output "server_names" {
  value = proxmox_virtual_environment_vm.servers[*].name
}

output "agent_names" {
  value = proxmox_virtual_environment_vm.agents[*].name
}

output "ansible_inventory" {
  description = "YAML inventory for Ansible RKE2/Swarm playbooks."
  value = yamlencode({
    all = {
      vars = {
        ansible_user                = "ubuntu"
        ansible_ssh_private_key_file = "~/.ssh/skstacks_deploy"
        cluster_name                = var.cluster_name
      }
      children = {
        rke2_servers = {
          hosts = {
            for vm in proxmox_virtual_environment_vm.servers :
            vm.name => {
              ansible_host = split("/", vm.initialization[0].ip_config[0].ipv4[0].address)[0]
              rke2_role    = "server"
            }
          }
        }
        rke2_agents = {
          hosts = {
            for vm in proxmox_virtual_environment_vm.agents :
            vm.name => {
              ansible_host = split("/", vm.initialization[0].ip_config[0].ipv4[0].address)[0]
              rke2_role    = "agent"
            }
          }
        }
      }
    }
  })
}
