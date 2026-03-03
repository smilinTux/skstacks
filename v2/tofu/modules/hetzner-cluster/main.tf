# SKStacks v2 — OpenTofu module: hetzner-cluster
#
# Provisions a set of Hetzner Cloud VMs ready for RKE2 or Docker Swarm.
# Creates: VMs, private network, SSH key, placement groups, firewalls.
#
# Usage:
#   module "cluster" {
#     source       = "../../modules/hetzner-cluster"
#     cluster_name = "skstack01"
#     env          = "prod"
#     hcloud_token = var.hcloud_token
#
#     server_count  = 3
#     agent_count   = 3
#     server_type   = "cx32"    # 4 vCPU, 8 GB RAM
#     location      = "fsn1"    # Falkenstein, Germany
#     image         = "ubuntu-24.04"
#   }

terraform {
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.47"
    }
    random = {
      source  = "opentofu/random"
      version = "~> 3.6"
    }
  }
  required_version = ">= 1.6.0"
}

provider "hcloud" {
  token = var.hcloud_token
}

# ── Local derived values ──────────────────────────────────────────────────────

locals {
  prefix      = "${var.cluster_name}-${var.env}"
  common_tags = merge(var.extra_tags, {
    cluster  = var.cluster_name
    env      = var.env
    managed  = "skstacks-tofu"
  })
}

# ── SSH Key ───────────────────────────────────────────────────────────────────

resource "hcloud_ssh_key" "deploy" {
  name       = "${local.prefix}-deploy"
  public_key = var.ssh_public_key
  labels     = local.common_tags
}

# ── Private network ───────────────────────────────────────────────────────────

resource "hcloud_network" "private" {
  name     = "${local.prefix}-private"
  ip_range = var.private_network_cidr
  labels   = local.common_tags
}

resource "hcloud_network_subnet" "nodes" {
  network_id   = hcloud_network.private.id
  type         = "cloud"
  network_zone = var.network_zone
  ip_range     = var.subnet_cidr
}

# ── Placement groups (spread across physical hosts) ───────────────────────────

resource "hcloud_placement_group" "servers" {
  name   = "${local.prefix}-servers"
  type   = "spread"
  labels = local.common_tags
}

resource "hcloud_placement_group" "agents" {
  count  = var.agent_count > 0 ? 1 : 0
  name   = "${local.prefix}-agents"
  type   = "spread"
  labels = local.common_tags
}

# ── Firewall ──────────────────────────────────────────────────────────────────

resource "hcloud_firewall" "nodes" {
  name   = "${local.prefix}-nodes"
  labels = local.common_tags

  # SSH — restrict to your management CIDR
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = var.ssh_allowed_cidrs
  }

  # HTTP/HTTPS (ingress)
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "80"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "443"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # RKE2 — kube-apiserver VIP (only from private network)
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "6443"
    source_ips = [var.private_network_cidr]
  }

  # RKE2 — supervisor API (join)
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "9345"
    source_ips = [var.private_network_cidr]
  }

  # etcd (internal only)
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "2379-2380"
    source_ips = [var.private_network_cidr]
  }

  # kubelet
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "10250"
    source_ips = [var.private_network_cidr]
  }

  # Canal VXLAN overlay
  rule {
    direction  = "in"
    protocol   = "udp"
    port       = "8472"
    source_ips = [var.private_network_cidr]
  }

  # ICMP (ping)
  rule {
    direction  = "in"
    protocol   = "icmp"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  # STUN/TURN (if coturn runs here)
  dynamic "rule" {
    for_each = var.enable_turn_ports ? [1] : []
    content {
      direction  = "in"
      protocol   = "udp"
      port       = "3478"
      source_ips = ["0.0.0.0/0", "::/0"]
    }
  }
}

# ── Server (control-plane) nodes ─────────────────────────────────────────────

resource "hcloud_server" "servers" {
  count = var.server_count

  name         = "${local.prefix}-server-${count.index + 1}"
  server_type  = var.server_type
  image        = var.image
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.deploy.id]
  firewall_ids = [hcloud_firewall.nodes.id]

  placement_group_id = hcloud_placement_group.servers.id

  labels = merge(local.common_tags, {
    role  = "server"
    index = tostring(count.index)
  })

  # Cloud-init: set hostname, install prerequisites
  user_data = templatefile("${path.module}/templates/cloud-init-server.yaml.tpl", {
    hostname     = "${local.prefix}-server-${count.index + 1}"
    ssh_pub_key  = var.ssh_public_key
    extra_packages = var.extra_packages
  })

  network {
    network_id = hcloud_network.private.id
    # Static private IP allocation: 10.0.1.x for servers
    ip         = cidrhost(var.subnet_cidr, count.index + 11)
  }

  lifecycle {
    ignore_changes = [user_data, image]   # Don't rebuild on image update
  }
}

# ── Agent (worker) nodes ──────────────────────────────────────────────────────

resource "hcloud_server" "agents" {
  count = var.agent_count

  name         = "${local.prefix}-agent-${count.index + 1}"
  server_type  = var.agent_server_type
  image        = var.image
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.deploy.id]
  firewall_ids = [hcloud_firewall.nodes.id]

  placement_group_id = var.agent_count > 0 ? hcloud_placement_group.agents[0].id : null

  labels = merge(local.common_tags, {
    role  = "agent"
    index = tostring(count.index)
  })

  user_data = templatefile("${path.module}/templates/cloud-init-agent.yaml.tpl", {
    hostname    = "${local.prefix}-agent-${count.index + 1}"
    ssh_pub_key = var.ssh_public_key
    extra_packages = var.extra_packages
  })

  network {
    network_id = hcloud_network.private.id
    # Static private IP allocation: 10.0.1.x for agents (after servers)
    ip         = cidrhost(var.subnet_cidr, count.index + 21)
  }

  lifecycle {
    ignore_changes = [user_data, image]
  }
}

# ── Floating IP (VIP equivalent for Hetzner) ──────────────────────────────────

resource "hcloud_floating_ip" "vip" {
  count     = var.create_floating_ip ? 1 : 0
  type      = "ipv4"
  home_location = var.location
  name      = "${local.prefix}-vip"
  labels    = local.common_tags
}

resource "hcloud_floating_ip_assignment" "vip" {
  count          = var.create_floating_ip ? 1 : 0
  floating_ip_id = hcloud_floating_ip.vip[0].id
  server_id      = hcloud_server.servers[0].id   # Initial assignment to first server
}
