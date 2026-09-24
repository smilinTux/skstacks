# SKStacks v2 — OpenTofu module: cloudflare-dns
#
# Manages DNS records for a SKStacks cluster deployment.
# Creates A/CNAME records for all core services.
#
# Usage:
#   module "dns" {
#     source             = "../../modules/cloudflare-dns"
#     cloudflare_token   = var.cloudflare_token
#     zone_id            = var.cloudflare_zone_id
#     cluster_name       = "skstack01"
#     domain             = "your-domain.com"
#     public_ip          = module.cluster.vip_address
#   }

terraform {
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.36"
    }
  }
  required_version = ">= 1.6.0"
}

provider "cloudflare" {
  api_token = var.cloudflare_token
}

locals {
  # All service subdomains to create as CNAMEs pointing to the cluster wildcard
  core_services = [
    "traefik",
    "crowdsec",
    "authentik",
    "grafana",
    "prometheus",
    "longhorn",
    "vault",
    "argocd",
  ]

  # Wildcard record for the cluster (all service subdomains resolve to this)
  cluster_fqdn = "${var.cluster_name}.${var.domain}"
}

# ── Cluster A record (points to VIP / floating IP) ───────────────────────────

resource "cloudflare_record" "cluster_root" {
  zone_id = var.zone_id
  name    = var.cluster_name
  value   = var.public_ip
  type    = "A"
  ttl     = var.ttl
  proxied = false   # NOT proxied — Traefik handles TLS termination

  comment = "SKStacks ${var.cluster_name} VIP — managed by OpenTofu"
}

# ── Wildcard record (*.skstack01.domain.com → cluster A record) ──────────────

resource "cloudflare_record" "cluster_wildcard" {
  zone_id = var.zone_id
  name    = "*.${var.cluster_name}"
  value   = "${var.cluster_name}.${var.domain}"
  type    = "CNAME"
  ttl     = var.ttl
  proxied = false

  comment = "SKStacks ${var.cluster_name} wildcard — managed by OpenTofu"
}

# ── Explicit records for services that need non-wildcard behavior ─────────────

resource "cloudflare_record" "apex" {
  count   = var.create_apex_record ? 1 : 0
  zone_id = var.zone_id
  name    = "@"
  value   = var.public_ip
  type    = "A"
  ttl     = var.ttl
  proxied = var.proxy_apex
}

resource "cloudflare_record" "www" {
  count   = var.create_www_record ? 1 : 0
  zone_id = var.zone_id
  name    = "www"
  value   = var.domain
  type    = "CNAME"
  ttl     = var.ttl
  proxied = var.proxy_apex
}

# ── TURN server record ────────────────────────────────────────────────────────

resource "cloudflare_record" "turn" {
  count   = var.create_turn_record ? 1 : 0
  zone_id = var.zone_id
  name    = "turn"
  value   = var.turn_ip != "" ? var.turn_ip : var.public_ip
  type    = "A"
  ttl     = var.ttl
  proxied = false   # STUN/TURN cannot be proxied through Cloudflare

  comment = "coturn STUN/TURN server — managed by OpenTofu"
}

# ── Additional custom records ─────────────────────────────────────────────────

resource "cloudflare_record" "extra" {
  for_each = { for r in var.extra_records : r.name => r }

  zone_id = var.zone_id
  name    = each.value.name
  value   = each.value.value
  type    = each.value.type
  ttl     = lookup(each.value, "ttl", var.ttl)
  proxied = lookup(each.value, "proxied", false)
  comment = lookup(each.value, "comment", "SKStacks extra record — managed by OpenTofu")
}

# ── Cloudflare Firewall / WAF rules ──────────────────────────────────────────

resource "cloudflare_ruleset" "cluster_protection" {
  count       = var.enable_waf_rules ? 1 : 0
  zone_id     = var.zone_id
  name        = "SKStacks ${var.cluster_name} protection"
  description = "Rate limiting and basic WAF for ${var.cluster_name}"
  kind        = "zone"
  phase       = "http_ratelimit"

  rules {
    description = "Rate limit API endpoints"
    expression  = "(http.request.uri.path matches \"^/api/\")"
    action      = "block"
    ratelimit {
      characteristics      = ["ip.src", "cf.colo.id"]
      period               = 60
      requests_per_period  = 100
      mitigation_timeout   = 300
    }
    enabled = true
  }
}
