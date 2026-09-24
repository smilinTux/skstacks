output "cluster_fqdn" {
  description = "The cluster FQDN, e.g. skstack01.your-domain.com"
  value       = "${var.cluster_name}.${var.domain}"
}

output "wildcard_fqdn" {
  description = "Wildcard FQDN for service subdomains."
  value       = "*.${var.cluster_name}.${var.domain}"
}

output "turn_fqdn" {
  description = "TURN server FQDN."
  value       = var.create_turn_record ? "turn.${var.domain}" : ""
}

output "nameservers" {
  description = "Cloudflare nameservers for this zone (display only)."
  value       = []   # Fetched via cloudflare_zone data source if needed
}
