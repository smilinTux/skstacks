# libvirt-cluster

Local KVM VMs for a Docker Swarm (or anything) cluster, via libvirt.

- Ubuntu Server 24.04 cloud image; cloud-init installs the HWE kernel and
  reboots once (the stock 6.8 guest kernel has shown NAT large-flow corruption
  under QEMU), plus Docker CE and `python3-docker`.
- A dedicated NAT network and storage pool per cluster; nothing bridged.
- Static addresses: managers `.11+`, workers `.21+`, so `ansible_inventory` is
  known at plan time. Groups match `v2/platform/swarm/ansible/inventory.example`.
- `worker_k3d = true` adds k3d and kubectl to workers.

## Optional apt cache (`apt_proxy`)

Every VM installs the HWE kernel, Docker and packages from the archives on each
run. Set `apt_proxy` to fetch them through a local caching proxy, for example an
apt-cacher-ng pull-through cache:

```hcl
apt_proxy = "http://apt-cache.example:3142"
```

Default is empty: no `apt:` key is rendered and cloud-init is byte-identical to
a module without this variable. The value must be `http://host[:port]` (no
credentials, no path, no https); anything else fails validation at plan time.

What it renders: cloud-init's native `apt:` key with `http_proxy` set to your
value, `Acquire::Retries "5"` (a cache filling for several VMs at once can
answer one request 503), `Acquire::https::Proxy "DIRECT"` and
`preserve_sources_list: true`, applied before
the package install runs. The install path is unchanged; only where bytes come
from changes.

- **What is cached.** Plain-HTTP apt sources, which is the Ubuntu archive and
  security mirrors on the default Noble image. This is the HWE kernel and the
  distro packages.
- **Docker's repo is not cached.** `download.docker.com` is https. An apt https
  proxy only tunnels (CONNECT), so it cannot cache, and it needs the proxy to
  allow CONNECT. The simplest correct choice is to leave that repo direct. Setting
  no https proxy is not enough: apt's https method falls back to the http proxy,
  and apt-cacher-ng answers that CONNECT with 403, so the Docker repo never loaded
  and Docker never installed (found on a test cluster, 2026-11-21). The module
  therefore pins `Acquire::https::Proxy "DIRECT"`. Caching it would mean rewriting the repo URL to the proxy's
  `http://HOST/HTTPS///download.docker.com/...` remap form and configuring the
  proxy to match, which changes the install path this feature promises to keep.
  The Docker GPG key is fetched with `curl` in `runcmd`, which ignores apt
  settings, so it is direct too. A base image whose sources are https is also
  fetched direct.
- **If the proxy is down.** Nothing waits forever, but nothing falls back to the
  archive either. apt fails every fetch: a refused connection fails at once, a
  dropped one waits for apt's connect timeout (default 120 s, per attempt).
  cloud-init logs the failed `package_update` and package install, continues
  with the remaining modules, runs `runcmd` (where the Docker `apt-get`
  steps fail the same way), and still reboots at the end. The VM comes up
  reachable over SSH without HWE or Docker, and `cloud-init status` reports
  `degraded` or `error`. Check the proxy and recreate the VMs. This is derived
  from apt and cloud-init behavior, not tested against a dead proxy.

Test: `tofu init -backend=false && tofu test` (mock provider, no libvirt needed).
