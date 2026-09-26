terraform {
  required_version = ">= 1.8.0"
  required_providers {
    libvirt = {
      source  = "dmacvicar/libvirt"
      version = "~> 0.8.3"
    }
  }
}

provider "libvirt" {
  uri = var.libvirt_uri
}

locals {
  prefix  = split("/", var.network_cidr)[1]
  gateway = cidrhost(var.network_cidr, 1)
  nodes = merge(
    { for i in range(var.manager_count) : "${var.cluster_name}-mgr-${i + 1}" => {
      ip = cidrhost(var.network_cidr, 11 + i), vcpu = var.manager_vcpu,
    mem = var.manager_memory_mb, disk = var.manager_disk_gb, worker = false } },
    { for i in range(var.worker_count) : "${var.cluster_name}-wkr-${i + 1}" => {
      ip = cidrhost(var.network_cidr, 21 + i), vcpu = var.worker_vcpu,
    mem = var.worker_memory_mb, disk = var.worker_disk_gb, worker = true } },
  )
}

resource "libvirt_pool" "this" {
  name = var.cluster_name
  type = "dir"
  target {
    path = "${var.pool_dir}/${var.cluster_name}"
  }
}

resource "libvirt_network" "this" {
  name      = var.cluster_name
  mode      = "nat"
  addresses = [var.network_cidr]
  autostart = true
  dhcp {
    enabled = false
  }
  dns {
    enabled = true
  }
}

resource "libvirt_volume" "base" {
  name   = "${var.cluster_name}-base.qcow2"
  pool   = libvirt_pool.this.name
  source = var.base_image_url
  format = "qcow2"
}

resource "libvirt_volume" "node" {
  for_each       = local.nodes
  name           = "${each.key}.qcow2"
  pool           = libvirt_pool.this.name
  base_volume_id = libvirt_volume.base.id
  size           = each.value.disk * 1024 * 1024 * 1024
  format         = "qcow2"
}

resource "libvirt_cloudinit_disk" "node" {
  for_each = local.nodes
  name     = "${each.key}-cloudinit.iso"
  pool     = libvirt_pool.this.name
  meta_data = yamlencode({
    "instance-id"    = each.key
    "local-hostname" = each.key
  })
  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    hostname       = each.key
    admin_user     = var.admin_user
    ssh_public_key = var.ssh_public_key
    k3d            = each.value.worker && var.worker_k3d
    daemon_json    = length(var.registry_mirrors) > 0 ? jsonencode({ "registry-mirrors" = var.registry_mirrors }) : ""
  })
  network_config = yamlencode({
    version = 2
    ethernets = {
      primary = {
        match       = { name = "en*" }
        addresses   = ["${each.value.ip}/${local.prefix}"]
        routes      = [{ to = "default", via = local.gateway }]
        nameservers = { addresses = [local.gateway] }
      }
    }
  })
}

resource "libvirt_domain" "node" {
  for_each   = local.nodes
  name       = each.key
  vcpu       = each.value.vcpu
  memory     = each.value.mem
  cloudinit  = libvirt_cloudinit_disk.node[each.key].id
  qemu_agent = false # static IPs; waiting on the agent would stall across the HWE reboot
  autostart  = false

  cpu {
    mode = "host-passthrough"
  }
  network_interface {
    network_id     = libvirt_network.this.id
    addresses      = [each.value.ip]
    wait_for_lease = false
  }
  disk {
    volume_id = libvirt_volume.node[each.key].id
  }
  console {
    type        = "pty"
    target_type = "serial"
    target_port = "0"
  }
}
