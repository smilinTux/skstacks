# skhub (Nextcloud)

Nextcloud plus its supporting services (MariaDB, Redis, ClamAV, cron,
notify_push, whiteboard, imaginary, and optional Collabora / Talk HPB) on
Docker Swarm.

## Storage backend

`skhub.storage_backend` controls where Nextcloud's file data lives:

| Value | Behaviour |
|---|---|
| unset / `local` (default) | Files live on `/var/data/skhub-<env>/data`, this instance's shared filesystem (NFS), matching production. No extra vault keys needed. |
| `s3` | Nextcloud's primary storage is an external S3-compatible object store you point at yourself: `skhub.s3_host`, `skhub.s3_access_key`, `skhub.s3_secret_key`, plus optional `skhub.s3_bucket_name` / `s3_port` / `s3_ssl` / `s3_usepath_style` / `s3_autocreate`. |
| `skstor` | A shortcut for `s3` against this cluster's own in-cluster [skstor](../skstor/README.md) (Garage) service. Host/port/region default to the in-cluster service (`skstor-<env>_garage`, port `3900`, no TLS, region `garage`) and this stack joins the `skstor-<env>` overlay network automatically. You still need `skhub.s3_access_key` / `skhub.s3_secret_key` from skstor's own one-time `garage key create` / `garage bucket allow` bootstrap (see skstor's README). |

**skstor must already be deployed** before you deploy skhub with
`storage_backend: skstor` -- its overlay network and Garage service have to
exist for skhub's compose to join them and reach it.

**Switching an existing instance is not supported by this playbook.**
Nextcloud reads `OBJECTSTORE_S3_*` only on first install (`occ` bootstrap);
changing `skhub.storage_backend` on a running instance does not migrate
already-uploaded files and will not be picked up without Nextcloud's own
`occ files:transfer-ownership`-style S3 migration tooling, which this
framework does not run. Set the backend before the first deploy of a new
instance.

## Minimal example: skhub with skstor as primary storage

```yaml
# skhub vault, in addition to the usual nextcloud_admin_*/mysql_* keys
skhub:
  storage_backend: skstor
  s3_access_key: "<from garage key create>"
  s3_secret_key: "<from garage key create>"
  # s3_bucket_name, s3_host, s3_port, s3_ssl, s3_usepath_style, s3_region
  # all default correctly for the in-cluster skstor service; override only
  # if you renamed the bucket or moved skstor off its defaults.
```
