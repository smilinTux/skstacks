# SKStacks v2 — OpenTofu state: S3 / MinIO backend
#
# Recommended for team use and CI/CD pipelines.
# Works with MinIO (self-hosted SKStor) and any S3-compatible store.
#
# Usage:
#   Copy this file to your example root and run:
#     tofu init \
#       -backend-config="bucket=skstacks-tofu-state" \
#       -backend-config="key=prod/skstack01/terraform.tfstate" \
#       -backend-config="endpoint=https://minio.your-domain.com" \
#       -backend-config="access_key=$MINIO_ACCESS_KEY" \
#       -backend-config="secret_key=$MINIO_SECRET_KEY"
#
# Prerequisites on MinIO/S3:
#   - Create bucket: skstacks-tofu-state
#   - Enable versioning (protects against accidental state deletion)
#   - Enable server-side encryption (AES-256 at rest)
#   - Restrict bucket policy to deploy IAM user only

terraform {
  backend "s3" {
    # All values injected at `tofu init` time — never hardcode here.

    bucket = "CHANGEME_STATE_BUCKET"
    key    = "CHANGEME_ENV/CHANGEME_CLUSTER/terraform.tfstate"

    # MinIO / S3-compatible endpoint (omit for AWS S3)
    endpoint = "CHANGEME_S3_ENDPOINT"     # e.g. https://minio.your-domain.com

    # For MinIO: force path-style (not virtual-hosted-style)
    force_path_style = true

    # AWS region (required field even for MinIO — use any value)
    region = "us-east-1"

    # Credentials via environment variables (preferred over hardcoding):
    #   AWS_ACCESS_KEY_ID     / access_key
    #   AWS_SECRET_ACCESS_KEY / secret_key

    # DynamoDB state lock table (optional for MinIO, use for AWS S3)
    # dynamodb_table = "skstacks-tofu-lock"

    # Skip AWS-specific validation for MinIO
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
  }

  # ── OpenTofu 1.7+ native state encryption ─────────────────────────────────
  # Encrypts state before uploading to the S3 backend.
  # The key is derived from a passphrase using PBKDF2 — store the passphrase
  # in your secret backend (vault-file / hashicorp-vault / capauth).
  #
  # To enable: uncomment the encryption block and set TF_ENCRYPTION env var,
  # or use -encryption-config flag.

  # encryption {
  #   key_provider "pbkdf2" "state_key" {
  #     passphrase = var.state_encryption_passphrase
  #   }
  #
  #   method "aes_gcm" "default" {
  #     keys = key_provider.pbkdf2.state_key
  #   }
  #
  #   state {
  #     method = method.aes_gcm.default
  #   }
  #
  #   plan {
  #     method = method.aes_gcm.default
  #   }
  # }
}
