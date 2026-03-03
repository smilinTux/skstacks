variable "cloudflare_token" {
  description = "Cloudflare API token with Zone:DNS:Edit permission."
  type        = string
  sensitive   = true
}

variable "zone_id" {
  description = "Cloudflare Zone ID for the target domain."
  type        = string
}

variable "cluster_name" {
  description = "Cluster identifier used as the DNS subdomain, e.g. 'skstack01'."
  type        = string
}

variable "domain" {
  description = "Root domain, e.g. 'your-domain.com'."
  type        = string
}

variable "public_ip" {
  description = "Public IP for the cluster VIP (A record target)."
  type        = string
}

variable "ttl" {
  description = "TTL for DNS records. 1 = automatic (when proxied = true)."
  type        = number
  default     = 300
}

variable "create_apex_record" {
  description = "Create an A record for the apex domain (@)."
  type        = bool
  default     = false
}

variable "create_www_record" {
  description = "Create a www CNAME."
  type        = bool
  default     = false
}

variable "proxy_apex" {
  description = "Proxy apex/www records through Cloudflare CDN."
  type        = bool
  default     = false
}

variable "create_turn_record" {
  description = "Create a 'turn' A record for the coturn STUN/TURN server."
  type        = bool
  default     = true
}

variable "turn_ip" {
  description = "Public IP for the TURN server. Leave empty to use the cluster VIP."
  type        = string
  default     = ""
}

variable "enable_waf_rules" {
  description = "Create Cloudflare WAF/ratelimit rules for the cluster."
  type        = bool
  default     = false
}

variable "extra_records" {
  description = "Additional DNS records to create."
  type = list(object({
    name    = string
    value   = string
    type    = string
    ttl     = optional(number)
    proxied = optional(bool)
    comment = optional(string)
  }))
  default = []
}
