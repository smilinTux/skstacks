# SKStacks v2 — OpenTofu module: proxmox-cluster
#
# Provisions VMs on Proxmox VE using cloud-init for sovereign on-premises deployments.
# Uses the Telmate/proxmox provider (community) or bpg/proxmox (recommended, actively maintained).
#
# Usage:
#   module "cluster" {
#     source            = "../../modules/proxmox-cluster"
#     proxmox_endpoint  = "https://proxmox.your-domain.com:8006"
#     proxmox_api_token = var.proxmox_api_token
#     cluster_name      = "skstack01"
#     env               = "prod"
#
#     server_count      = 3
#     agent_count       = 3
#     vm_template       = "ubuntu-24.04-cloud"
#     datastore         = "local-lvm"
#     node              = "pve"
#   }

terraform {
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.51"
    }
    random = {
      source  = "opentofu/random"
      version = "~> 3.6"
    }
  }
  required_version = ">= 1.6.0"
}

provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token
  insecure  = var.proxmox_skip_tls_verify    # false in production

  ssh {
    agent    = true                           # use SSH agent for host operations
    username = var.proxmox_ssh_user
  }
}

locals {
  prefix = "${var.cluster_name}-${var.env}"

  # VM ID ranges (Proxmox requires unique integer IDs)
  server_vmid_start = 1000 + (var.env == "prod" ? 0 : var.env == "staging" ? 100 : 200)
  agent_vmid_start  = 1050 + (var.env == "prod" ? 0 : var.env == "staging" ? 100 : 200)
}

# ── Server (control-plane) VMs ────────────────────────────────────────────────

resource "proxmox_virtual_environment_vm" "servers" {
  count = var.server_count

  name      = "${local.prefix}-server-${count.index + 1}"
  node_name = var.proxmox_node
  vm_id     = local.server_vmid_start + count.index

  tags = ["skstacks", var.cluster_name, var.env, "server"]

  clone {
    vm_id = var.vm_template_id
    full  = true             # full clone (not linked) for isolation
  }

  cpu {
    cores  = var.server_cores
    type   = "x86-64-v2-AES"  # modern CPU with AES-NI
  }

  memory {
    dedicated = var.server_memory_mb
  }

  disk {
    datastore_id = var.datastore
    size         = var.server_disk_gb
    interface    = "scsi0"
    iothread     = true      # improves IOPS on NVMe-backed storage
    discard      = "on"      # TRIM support
  }

  # Additional disk for Longhorn (separate from OS disk)
  dynamic "disk" {
    for_each = var.longhorn_disk_gb > 0 ? [1] : []
    content {
      datastore_id = var.datastore
      size         = var.longhorn_disk_gb
      interface    = "scsi1"
      iothread     = true
      discard      = "on"
    }
  }

  network_device {
    bridge  = var.bridge
    model   = "virtio"
    vlan_id = var.vlan_id
  }

  operating_system {
    type = "l26"   # Linux 2.6+
  }

  agent {
    enabled = true   # requires qemu-guest-agent in template
  }

  # Cloud-init configuration
  initialization {
    ip_config {
      ipv4 {
        address = "${cidrhost(var.server_subnet_cidr, count.index + 11)}/${split("/", var.server_subnet_cidr)[1]}"
        gateway = var.gateway
      }
    }

    dns {
      servers = var.dns_servers
    }

    user_account {
      username = "ubuntu"
      keys     = [var.ssh_public_key]
    }

    user_data_file_id = proxmox_virtual_environment_file.cloud_init_server[count.index].id
  }

  lifecycle {
    ignore_changes = [clone, initialization]
  }
}

# Upload cloud-init snippets to Proxmox
resource "proxmox_virtual_environment_file" "cloud_init_server" {
  count         = var.server_count
  content_type  = "snippets"
  datastore_id  = var.snippets_datastore
  node_name     = var.proxmox_node

  source_raw {
    file_name = "${local.prefix}-server-${count.index + 1}-cloud-init.yaml"
    data = templatefile("${path.module}/templates/cloud-init-node.yaml.tpl", {
      hostname    = "${local.prefix}-server-${count.index + 1}"
      ssh_pub_key = var.ssh_public_key
    })
  }
}

# ── Agent (worker) VMs ────────────────────────────────────────────────────────

resource "proxmox_virtual_environment_vm" "agents" {
  count = var.agent_count

  name      = "${local.prefix}-agent-${count.index + 1}"
  node_name = var.proxmox_node
  vm_id     = local.agent_vmid_start + count.index

  tags = ["skstacks", var.cluster_name, var.env, "agent"]

  clone {
    vm_id = var.vm_template_id
    full  = true
  }

  cpu {
    cores = var.agent_cores
    type  = "x86-64-v2-AES"
  }

  memory {
    dedicated = var.agent_memory_mb
  }

  disk {
    datastore_id = var.datastore
    size         = var.agent_disk_gb
    interface    = "scsi0"
    iothread     = true
    discard      = "on"
  }

  dynamic "disk" {
    for_each = var.longhorn_disk_gb > 0 ? [1] : []
    content {
      datastore_id = var.datastore
      size         = var.longhorn_disk_gb
      interface    = "scsi1"
      iothread     = true
      discard      = "on"
    }
  }

  network_device {
    bridge  = var.bridge
    model   = "virtio"
    vlan_id = var.vlan_id
  }

  operating_system {
    type = "l26"
  }

  agent {
    enabled = true
  }

  initialization {
    ip_config {
      ipv4 {
        address = "${cidrhost(var.agent_subnet_cidr, count.index + 11)}/${split("/", var.agent_subnet_cidr)[1]}"
        gateway = var.gateway
      }
    }
    dns {
      servers = var.dns_servers
    }
    user_account {
      username = "ubuntu"
      keys     = [var.ssh_public_key]
    }
    user_data_file_id = proxmox_virtual_environment_file.cloud_init_agent[count.index].id
  }

  lifecycle {
    ignore_changes = [clone, initialization]
  }
}

resource "proxmox_virtual_environment_file" "cloud_init_agent" {
  count         = var.agent_count
  content_type  = "snippets"
  datastore_id  = var.snippets_datastore
  node_name     = var.proxmox_node

  source_raw {
    file_name = "${local.prefix}-agent-${count.index + 1}-cloud-init.yaml"
    data = templatefile("${path.module}/templates/cloud-init-node.yaml.tpl", {
      hostname    = "${local.prefix}-agent-${count.index + 1}"
      ssh_pub_key = var.ssh_public_key
    })
  }
}
