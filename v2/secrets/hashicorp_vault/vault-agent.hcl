# Vault Agent configuration — sidecar template rendering
#
# Used for non-K8s deployments (Docker Swarm, bare metal) where the
# Vault Agent sidecar cannot be auto-injected.
#
# Run alongside each service:
#   vault agent -config=/etc/vault-agent/config.hcl
#
# The agent authenticates to Vault using AppRole, then renders
# secret templates to files that the service process reads.

pid_file = "/run/vault-agent.pid"

vault {
  address = "https://vault.CHANGEME_DOMAIN:8200"
  # tls_ca_cert   = "/etc/vault-agent/ca.crt"   # Uncomment for custom CA
  retry {
    num_retries = 5
  }
}

# AppRole auth (secrets stored in files mounted from secure locations)
auto_auth {
  method "approle" {
    mount_path = "auth/approle"
    config = {
      role_id_file_path   = "/run/secrets/vault-role-id"
      secret_id_file_path = "/run/secrets/vault-secret-id"
      remove_secret_id_file_after_reading = false
    }
  }

  sink "file" {
    config = {
      path = "/run/vault-agent/token"
      mode = 0640
    }
  }
}

# Cache — avoids re-requesting secrets on each template render
cache {
  use_auto_auth_token = true
}

# Template blocks — render secrets to ephemeral files
# The service reads from /run/secrets/ which lives in a tmpfs

template {
  source      = "/etc/vault-agent/templates/skfence.env.ctmpl"
  destination = "/run/secrets/skfence.env"
  perms       = 0600
  command     = "kill -HUP $(cat /run/skfence.pid) 2>/dev/null || true"
  error_on_missing_key = true
}

# ── Example template file: /etc/vault-agent/templates/skfence.env.ctmpl ──
#
# {{ with secret "kv/data/skstacks/prod/skfence/cloudflare_dns_token" }}
# CLOUDFLARE_DNS_TOKEN={{ .Data.data.value }}
# {{ end }}
# {{ with secret "kv/data/skstacks/prod/skfence/dashboard_password_hash" }}
# DASHBOARD_PASSWORD_HASH={{ .Data.data.value }}
# {{ end }}
