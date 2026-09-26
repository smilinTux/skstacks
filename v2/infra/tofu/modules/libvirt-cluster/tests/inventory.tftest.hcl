mock_provider "libvirt" {}

variables {
  cluster_name   = "t06"
  env            = "dev"
  network_cidr   = "10.99.0.0/24"
  ssh_public_key = "ssh-ed25519 AAAA test"
}

run "inventory_groups" {
  command = plan
  assert {
    condition     = strcontains(output.ansible_inventory, "[swarm_manager_primary]\nt06-mgr-1 ansible_host=10.99.0.11")
    error_message = "primary manager group wrong"
  }
  assert {
    condition     = strcontains(output.ansible_inventory, "t06-mgr-3 ansible_host=10.99.0.13") && strcontains(output.ansible_inventory, "[swarm_workers]\nt06-wkr-1 ansible_host=10.99.0.21")
    error_message = "manager/worker addressing wrong"
  }
}

run "worker_count_zero" {
  command = plan
  variables {
    worker_count = 0
  }
  assert {
    condition     = !strcontains(output.ansible_inventory, "-wkr-")
    error_message = "no workers expected"
  }
}

run "registry_mirrors_written_to_daemon_json" {
  command = plan
  variables {
    registry_mirrors = ["https://mirror.example.test"]
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "\"registry-mirrors\":[\"https://mirror.example.test\"]")
    error_message = "daemon.json registry-mirrors missing"
  }
}

run "no_mirrors_by_default" {
  command = plan
  assert {
    condition     = !strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "registry-mirrors")
    error_message = "mirrors must be opt-in"
  }
}
