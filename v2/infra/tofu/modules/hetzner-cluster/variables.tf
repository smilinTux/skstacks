variable "hcloud_token" {
  description = "Hetzner Cloud API token. SENSITIVE — read from secret backend, never hardcode."
  type        = string
  sensitive   = true
}

variable "cluster_name" {
  description = "Short cluster identifier, e.g. 'skstack01'. Used as resource name prefix."
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
  description = "SSH public key to inject into all nodes."
  type        = string
}

variable "ssh_allowed_cidrs" {
  description = "CIDR blocks allowed to SSH. Restrict to your management IP(s)."
  type        = list(string)
  default     = ["0.0.0.0/0"]   # Override this in production!
}

variable "server_count" {
  description = "Number of RKE2 server (control-plane) nodes. Must be odd (1, 3, or 5) for etcd HA."
  type        = number
  default     = 3
  validation {
    condition     = contains([1, 3, 5], var.server_count)
    error_message = "server_count must be 1, 3, or 5 (etcd quorum requirement)."
  }
}

variable "agent_count" {
  description = "Number of RKE2 agent (worker) nodes. 0 = control-plane nodes also run workloads."
  type        = number
  default     = 3
}

variable "server_type" {
  description = "Hetzner server type for control-plane nodes."
  type        = string
  default     = "cx32"   # 4 vCPU / 8 GB / 80 GB disk
}

variable "agent_server_type" {
  description = "Hetzner server type for worker nodes."
  type        = string
  default     = "cx42"   # 8 vCPU / 16 GB / 160 GB disk
}

variable "image" {
  description = "OS image for all nodes."
  type        = string
  default     = "ubuntu-24.04"
}

variable "location" {
  description = "Hetzner datacenter location."
  type        = string
  default     = "fsn1"   # Falkenstein, Germany
  # Other options: nbg1 (Nuremberg), hel1 (Helsinki), ash (Ashburn), hil (Hillsboro)
}

variable "network_zone" {
  description = "Hetzner network zone (must match location)."
  type        = string
  default     = "eu-central"
}

variable "private_network_cidr" {
  description = "Private network CIDR block."
  type        = string
  default     = "10.0.0.0/8"
}

variable "subnet_cidr" {
  description = "Subnet CIDR for node private IPs."
  type        = string
  default     = "10.0.1.0/24"
}

variable "create_floating_ip" {
  description = "Create a Hetzner Floating IP to serve as the API VIP."
  type        = bool
  default     = true
}

variable "enable_turn_ports" {
  description = "Open STUN/TURN ports in the firewall (needed if running coturn on these nodes)."
  type        = bool
  default     = false
}

variable "extra_packages" {
  description = "Additional apt packages to install via cloud-init."
  type        = list(string)
  default     = []
}

variable "extra_tags" {
  description = "Additional labels to apply to all Hetzner resources."
  type        = map(string)
  default     = {}
}
