# Decision: skstor's S3 backend, post-MinIO

**Status: recommended, needs Chef decision to merge.** No `docs/decisions/`
convention existed in this repo before this file; starting one here.

## Problem

`v1/ansible/optional/skstor` (single-node MinIO on Docker Swarm) is parked out
of the framework move because its image is gone. Verified live 2026-09-25:

- `docker manifest inspect quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z`
  (the tag prod actually pins) returns `no such manifest`.
- MinIO Community Edition's GitHub repo was archived 2026-04-25 (read-only,
  "THIS REPOSITORY IS NO LONGER MAINTAINED" since 2026-02-12). On
  2026-09-11 MinIO deleted `minio/minio` and `minio/mc` from Docker Hub
  outright. The push is toward the commercial AIStor product
  (~$0.02/GB/month, enterprise self-service quoted around $96k/yr for
  400 TiB). Historical quay.io tags exist for *some* releases but receive no
  further security patches, and the exact tag prod pinned is already gone.

This is not a version bump, it is a dead upstream.

## This was already decided once, elsewhere in this repo

`v2/migrations/minio-to-garage.md`, `v2/compute/SKSTORAGE.md`, and
`v2/compute/skobject/` + `v2/platform/swarm/stacks/skobject/docker-compose.yml`
already ratify **Garage** as the object-storage adapter for the v2 platform
layer (the compose file's own header literally says `RATIFIED: Garage; MinIO
archived Apr 2026`). This document extends that same decision down to the
legacy v1 Ansible/Swarm layer, so the framework has one answer, not two.

## Candidates evaluated (verified live 2026-09-25)

| Option | Image / license | S3 fit for our consumers | Swarm fit | Verdict |
|---|---|---|---|---|
| **Garage** (dxflrs/garage) | `dxflrs/garage:v2.4.1`, multi-arch, pullable anonymously. AGPLv3, Deuxfleurs (non-profit, small self-hosting collective; not a bait-and-switch vendor). | S3 API covers what skhub (s3fs mount), skform (tofu S3 backend), skblock/skfile (JuiceFS backing) need: buckets, multipart, presigned. No bucket versioning/Object-Lock. | `replication_factor=1` single node (drop-in for today's single-manager skstor) or `=3` for HA later; no CSI/K8s needed. | **Recommended.** Already the ratified v2 answer; single node today, same binary scales to the v2 pattern later. |
| SeaweedFS | `chrislusf/seaweedfs`, multi-arch, pullable. Apache-2.0. | Broader S3 coverage, incl. Object-Lock/WORM (Garage lacks this per `SKSTORAGE.md`). | Needs master+volume(+filer) topology; heavier for one node. | Keep as the fallback **only if** a consumer needs WORM/Object-Lock immutability. None of skhub/skform/skblock/skfile need it today. |
| Chainguard MinIO (`cgr.dev/chainguard/minio`) | Image exists and pulls today. | Same MinIO S3 API prod already depends on (zero consumer-side change). | Drop-in replacement for the exact same compose. | **Rejected.** Chainguard's free/public tier does not reliably retain superseded digests (their own stated "we don't delete images" policy conflicts with observed free-tier GC of old digests on routine rebuilds) — pinning by digest, the whole point of this exercise, is not dependable on the free tier. It is also still MinIO upstream, i.e. still subject to the same commercial-pressure trajectory that killed the previous image. |
| Bitnami / other MinIO forks, pinned last-available tag | Exists as a frozen artifact only. | Same MinIO API. | Drop-in. | **Rejected.** A frozen last-known-good tag never gets a CVE patch again; this only buys time, it doesn't solve the problem, and Bitnami's own catalog has been through its own restructuring (paid-only images for many tags) this year. |

## Recommendation

**Garage**, `dxflrs/garage@sha256:9c96caa2612d3411acc5b0e6701fb238dbfba33e533a6d7d3d811a4b12d0d020`
(= the `v2.4.1` manifest list, verified live via the Docker Hub registry API
2026-09-25), single node (`replication_factor = 1`) matching today's
single-manager skstor deploy pattern.

Reasons, in order:
1. **Already ratified for v2** — one backend, not two, across the framework.
2. **Free-tier-durable pinning** — Docker Hub keeps `dxflrs/garage` tags
   (unlike Chainguard's free tier), and AGPLv3/Deuxfleurs is not a vendor with
   a commercial upsell to retreat toward.
3. **No consumer needs what only SeaweedFS offers** (Object-Lock/WORM); Garage
   covers skhub/skform/skblock/skfile today and this same binary is the
   scale-up path to the v2 skobject pattern (RF=3, multi-node) if/when this
   estate grows workers.

## Consumer impact (prod, `~/work/smilinTux/SKStacks`)

- **skhub** (`v1/ansible/optional/skhub/src/skhub/mnt/setup-s3fs-data.sh.j2`):
  s3fs mount using `s3_access_key`/`s3_secret_key` from vault. Same s3fs
  config works against Garage; the access/secret pair must be a Garage `garage
  key create` key instead of the MinIO root user/password.
- **skform** (`v1/ansible/optional/skform/src/config/skform/tofu.hcl.j2`,
  `v2/infra/tofu/state/s3-backend.tf`): OpenTofu S3 backend, `force_path_style
  = true`, region is a free-text value for a non-AWS S3. Compatible as-is.
  Note: the backend doc recommends enabling bucket **versioning** on the state
  bucket; Garage does not support S3 bucket versioning. State-loss protection
  should come from a periodic backup of the bucket (rclone/mc mirror to a
  second target, or your own snapshot job) instead of relying on
  Garage-side versioning.
- **skblock/skfile** (`v2/compute/skblock/app.yaml`, `v2/compute/skfile/app.yaml`):
  already describe Garage/SeaweedFS as `skobject`'s backing store; no change.
- **Biggest operational difference**: MinIO's root user/password functioned as
  a single set of admin S3 credentials every consumer reused. Garage has no
  such root credential — each consumer needs its own key
  (`garage key create <name>`) granted to its own bucket
  (`garage bucket create <bucket>; garage bucket allow --read --write <bucket>
  --key <name>`), a one-time manual step per consumer per environment. See
  `v1/ansible/optional/skstor/README.md` (this PR) for the exact commands.
- No bucket needs Object-Lock/WORM today; revisit SeaweedFS if that changes.

## What a skstack06 test stage needs

- A fresh skstor-{env} deploy on an idle manager, confirming `garage status`
  reports the single node healthy after the one-time `garage layout assign
  -z <zone> -c <capacity> <node-id>` + `garage layout apply --version N`
  bootstrap (this replaces MinIO's zero-config boot; Garage refuses S3 traffic
  until a layout is applied).
- One `garage key create` + `garage bucket create` + `garage bucket allow`
  cycle, then a real `mc`/`aws s3` or `rclone` read/write smoke test against
  the bucket over the `skstor{-env}.<cluster>.<domain>` Traefik hostname, the
  same way `test_env_playbook_parity.py`-style stages already smoke other
  optional services.
- No change to skstack06's network/subnet allocation beyond the
  `skstor-<env>` / `172.16.80.0/24` overlay this module registers.
