variable "cluster_name" {
  description = "Cluster name; prefixes every libvirt object and node name."
  type        = string
}
variable "env" {
  description = "Environment label (dev, staging, prod)."
  type        = string
}
variable "network_cidr" {
  description = "CIDR of the dedicated libvirt NAT network. Managers get .11+, workers .21+."
  type        = string
}
variable "ssh_public_key" {
  description = "Public key installed for admin_user on every node."
  type        = string
}
variable "admin_user" {
  type    = string
  default = "skadmin"
}
variable "manager_count" {
  type    = number
  default = 3
}
variable "worker_count" {
  type    = number
  default = 1
}
variable "manager_vcpu" {
  type    = number
  default = 2
}
variable "worker_vcpu" {
  type    = number
  default = 2
}
variable "manager_memory_mb" {
  type    = number
  default = 2560
}
variable "worker_memory_mb" {
  type    = number
  default = 3072
}
variable "manager_disk_gb" {
  type    = number
  default = 20
}
variable "worker_disk_gb" {
  type    = number
  default = 30
}
variable "worker_k3d" {
  description = "Install k3d + kubectl on workers (for k3d smoke tests inside the VM)."
  type        = bool
  default     = false
}
variable "base_image_url" {
  description = "Cloud image. Ubuntu Server 24.04 LTS; cloud-init adds the HWE kernel."
  type        = string
  default     = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
}
variable "libvirt_uri" {
  type    = string
  default = "qemu:///system"
}
variable "pool_dir" {
  description = "Directory for the cluster's storage pool."
  type        = string
  default     = "/var/lib/libvirt/images"
}
variable "registry_mirrors" {
  description = "Docker registry mirrors written to /etc/docker/daemon.json (e.g. [\"https://mirror.gcr.io\"] to avoid Docker Hub anonymous rate limits). Empty = none."
  type        = list(string)
  default     = []
}
variable "disable_auto_updates" {
  description = "Disable unattended-upgrades, the apt-daily timers/services, needrestart's auto-restart, motd-news, and snapd auto-refresh via cloud-init, before the HWE kernel package install runs. For a short-lived test cluster, a background reboot mid-run is pure risk. Default false renders byte-identical cloud-init to today."
  type        = bool
  default     = false
}
variable "apt_proxy" {
  description = "Optional HTTP proxy for apt on every VM, as http://host[:port] (e.g. \"http://apt-cache.example:3142\" for an apt-cacher-ng pull-through cache). Rendered through cloud-init's native apt: key before the package install runs. Only plain-HTTP apt sources are cached (the Ubuntu archive); https sources such as the Docker repo are fetched direct (https_proxy: DIRECT). Empty = no proxy, and the rendered cloud-init is byte-identical to a module without this variable. No credentials in the URL."
  type        = string
  default     = ""
  validation {
    condition     = var.apt_proxy == "" || can(regex("^http://([A-Za-z0-9.-]+|\\[[0-9A-Fa-f:]+\\])(:[0-9]{1,5})?/?$", var.apt_proxy))
    error_message = "apt_proxy must be empty or an http://host[:port] URL (no https, no credentials, no path)."
  }
}
