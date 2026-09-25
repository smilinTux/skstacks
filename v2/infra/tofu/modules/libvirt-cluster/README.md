# libvirt-cluster

Local KVM VMs for a Docker Swarm (or anything) cluster, via libvirt.

- Ubuntu Server 24.04 cloud image; cloud-init installs the HWE kernel and
  reboots once (the stock 6.8 guest kernel has shown NAT large-flow corruption
  under QEMU), plus Docker CE and `python3-docker`.
- A dedicated NAT network and storage pool per cluster; nothing bridged.
- Static addresses: managers `.11+`, workers `.21+`, so `ansible_inventory` is
  known at plan time. Groups match `v2/platform/swarm/ansible/inventory.example`.
- `worker_k3d = true` adds k3d and kubectl to workers.

Test: `tofu init -backend=false && tofu test` (mock provider, no libvirt needed).
