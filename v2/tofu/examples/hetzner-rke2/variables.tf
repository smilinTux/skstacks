variable "hcloud_token" {
  type      = string
  sensitive = true
  default   = ""   # Override via TF_VAR_hcloud_token or vault-file-wrapper.sh
}

variable "cloudflare_api_token" {
  type      = string
  sensitive = true
  default   = ""
}

variable "cloudflare_zone_id" {
  type    = string
  default = ""
}

variable "cluster_name" {
  type = string
}

variable "env" {
  type    = string
  default = "prod"
}

variable "domain" {
  type = string
}

variable "ssh_public_key" {
  type = string
}

variable "ssh_allowed_cidrs" {
  type    = list(string)
  default = ["0.0.0.0/0"]
}

variable "server_count" {
  type    = number
  default = 3
}

variable "server_type" {
  type    = string
  default = "cx32"
}

variable "agent_count" {
  type    = number
  default = 3
}

variable "agent_server_type" {
  type    = string
  default = "cx42"
}

variable "image" {
  type    = string
  default = "ubuntu-24.04"
}

variable "location" {
  type    = string
  default = "fsn1"
}

variable "deploy_coturn" {
  type    = bool
  default = false
}

variable "enable_cloudflare_waf" {
  type    = bool
  default = false
}
