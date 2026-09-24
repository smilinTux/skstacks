variable "proxmox_endpoint" {
  description = "Proxmox API URL, e.g. https://proxmox.your-domain.com:8006"
  type        = string
}

variable "proxmox_api_token" {
  description = "Proxmox API token. Format: user@pam!token-name=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  type        = string
  sensitive   = true
}

variable "proxmox_skip_tls_verify" {
  description = "Skip TLS verification for Proxmox API. Set false in production."
  type        = bool
  default     = false
}

variable "proxmox_node" {
  description = "Proxmox node name to deploy VMs on."
  type        = string
  default     = "pve"
}

variable "proxmox_ssh_user" {
  description = "SSH user for Proxmox host operations (used by provider for file uploads)."
  type        = string
  default     = "root"
}

variable "cluster_name" {
  description = "Short cluster identifier, e.g. 'skstack01'."
  type        = string
}

variable "env" {
  description = "Environment: prod, staging, or dev."
  type        = string
  validation {
    condition     = contains(["prod", "staging", "dev"], var.env)
    error_message = "env must be prod, staging, or dev."
  }
}

variable "ssh_public_key" {
  description = "SSH public key to inject into VMs."
  type        = string
}

variable "vm_template_id" {
  description = "Proxmox VM ID of the cloud-init template to clone. Create with virt-customize or Packer."
  type        = number
  # Example: 9000 (ubuntu-24.04 template)
}

variable "datastore" {
  description = "Proxmox storage name for VM disks, e.g. 'local-lvm', 'ceph-pool'."
  type        = string
  default     = "local-lvm"
}

variable "snippets_datastore" {
  description = "Proxmox storage for cloud-init snippets. Must have 'snippets' content type enabled."
  type        = string
  default     = "local"
}

variable "bridge" {
  description = "Proxmox network bridge for VM NICs, e.g. 'vmbr0'."
  type        = string
  default     = "vmbr0"
}

variable "vlan_id" {
  description = "Optional VLAN tag for VM network. 0 = untagged."
  type        = number
  default     = 0
}

variable "gateway" {
  description = "Default gateway for VMs."
  type        = string
}

variable "dns_servers" {
  description = "DNS servers for VMs."
  type        = list(string)
  default     = ["1.1.1.1", "1.0.0.1"]
}

variable "server_count" {
  type    = number
  default = 3
  validation {
    condition     = contains([1, 3, 5], var.server_count)
    error_message = "server_count must be 1, 3, or 5."
  }
}

variable "agent_count" {
  type    = number
  default = 3
}

variable "server_cores" {
  type    = number
  default = 4
}

variable "server_memory_mb" {
  type    = number
  default = 8192
}

variable "server_disk_gb" {
  type    = number
  default = 80
}

variable "agent_cores" {
  type    = number
  default = 8
}

variable "agent_memory_mb" {
  type    = number
  default = 16384
}

variable "agent_disk_gb" {
  type    = number
  default = 150
}

variable "longhorn_disk_gb" {
  description = "Size of a dedicated Longhorn data disk added to each VM. 0 = disabled."
  type        = number
  default     = 200
}

variable "server_subnet_cidr" {
  description = "Subnet CIDR for server node IPs."
  type        = string
  default     = "192.168.1.0/24"
}

variable "agent_subnet_cidr" {
  description = "Subnet CIDR for agent node IPs."
  type        = string
  default     = "192.168.1.0/24"
}
