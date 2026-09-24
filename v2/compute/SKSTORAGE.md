# skstorage — HA storage family (skblock · skfile · skobject)

Storage has three planes with genuinely different contracts, so it's modeled as
three sibling ports (not one mega-port), all under `compute/`:

| Port | Plane | Default adapter | Scale-up | Swarm (no CSI) |
|---|---|---|---|---|
| `skblock` | Block, RWO (DBs/StatefulSets) | **Longhorn** | Mayastor (NVMe) · Rook-Ceph-RBD | host-local + app-HA (Patroni) · LINSTOR/DRBD |
| `skfile` | Shared file, RWX POSIX | **JuiceFS** (on `skobject` + `skdata`) | CephFS · SeaweedFS-filer | JuiceFS FUSE mount |
| `skobject` | Object, S3 | **Garage** (RF=3) | SeaweedFS (WORM) · Ceph-RGW | Garage/SeaweedFS containers |

## The sovereign move: JuiceFS as adapter composition
`skfile`'s default adapter doesn't stand up a second clustered filesystem — it
**layers HA POSIX-RWX on the object plane you already run**:
- **data** → `skobject` (Garage/SeaweedFS S3)
- **metadata** → `skdata` (PostgreSQL 17 / skmem-pg), made HA with Patroni

The resolver wires `skfile → {skobject, skdata}` from the `depends_on` field.
The metadata engine is a hard dependency and single consistency domain, so it
must itself be HA.

## Backup-target convention
Every storage adapter ships snapshots/backups to the object plane:
`backup-target = skobject`. Longhorn → S3, JuiceFS dumps → S3, etc.

## The crossover to Rook-Ceph
Stay disaggregated (Longhorn + JuiceFS + Garage) while ≤5–7 nodes or
memory-constrained. **Consolidate onto Rook-Ceph only** when you have ≥5–6
dedicated-disk K8s nodes (≥64 GB RAM each), need all three planes at once, want
one replication domain, and have someone to own Ceph. Ceph is **not** a Swarm
option — consolidation implies you've standardized on K8s/RKE2.

> 2026 notes: MinIO archived (out); GlusterFS EOL'd by Red Hat 2024 (avoid for
> new builds); JuiceFS CE is Apache-2.0; Mayastor v2.11 GA-grade; Longhorn v2
> (SPDK) GA but young — keep v1 as the stable default.


## Swarm host-local convention (`/var/local/data`)
For Swarm stateful engines on the **host-local + app-HA** path (skblock row: Postgres/Patroni,
Qdrant, FalkorDB), node-local state lives at **`/var/local/data/{service}-{env}/...`** (dedicated
local SSD, mirrors the NFS `/var/data/...` layout) — never on NFS `/var/data` (mmap+locking
corruption risk). Node-pinned or app-replicated; snapshots ship to the object plane
(backup-target = skobject) or into `/var/data/.../database-dump` for Duplicati. Full standard:
`v1/docs/VOLUME_MANAGEMENT_STANDARDS.md`.
