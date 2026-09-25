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
