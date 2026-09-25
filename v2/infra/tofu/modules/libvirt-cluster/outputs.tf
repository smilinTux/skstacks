locals {
  managers = { for k, v in local.nodes : k => v if !v.worker }
  workers  = { for k, v in local.nodes : k => v if v.worker }
  mgr_keys = sort(keys(local.managers))
}

output "manager_ips" {
  value = [for k in local.mgr_keys : local.managers[k].ip]
}

output "worker_ips" {
  value = [for k in sort(keys(local.workers)) : local.workers[k].ip]
}

output "ansible_inventory" {
  description = "INI inventory in the group layout of v2/platform/swarm/ansible/inventory.example."
  value = join("\n", concat(
    ["[swarm_manager_primary]", "${local.mgr_keys[0]} ansible_host=${local.managers[local.mgr_keys[0]].ip}", ""],
    ["[swarm_managers]"], [for k in local.mgr_keys : "${k} ansible_host=${local.managers[k].ip}"], [""],
    ["[swarm_workers]"], [for k in sort(keys(local.workers)) : "${k} ansible_host=${local.workers[k].ip}"], [""],
    ["[all:vars]", "ansible_user=${var.admin_user}", ""],
  ))
}
