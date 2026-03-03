# SKStacks v2 — OpenTofu state: HTTP backend (Forgejo-native)
#
# Forgejo supports the Terraform HTTP state backend natively.
# No extra infra needed if you already run SKGit (Forgejo).
#
# Usage:
#   tofu init \
#     -backend-config="address=https://git.your-domain.com/api/v1/repos/ORG/skstacks/raw/state/prod.tfstate" \
#     -backend-config="username=$FORGEJO_USER" \
#     -backend-config="password=$FORGEJO_TOKEN"
#
# Note: Forgejo does not support state locking via the HTTP backend.
#       For team use with concurrent applies, prefer the S3 backend.

terraform {
  backend "http" {
    # All values injected at `tofu init` — never hardcode here.

    address        = "CHANGEME_FORGEJO_STATE_URL"
    # e.g. https://git.your-domain.com/api/v1/repos/CHANGEME_ORG/skstacks/raw/state/prod-CHANGEME_CLUSTER.tfstate

    lock_address   = ""   # Forgejo does not support locking — leave empty
    unlock_address = ""

    # Credentials via env vars:
    #   TF_HTTP_USERNAME  = Forgejo username
    #   TF_HTTP_PASSWORD  = Forgejo personal access token
  }
}
