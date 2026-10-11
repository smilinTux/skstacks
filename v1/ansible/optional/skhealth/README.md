# skhealth

Lightweight infrastructure health checks with alerts: swarm services and
nodes, filesystems and NFS mounts, memory and load, OOM kills, failed systemd
units, local disk SMART health and NVMe wear, ZFS pools and md RAID, read-only
checks of extra hosts (a NAS or Proxmox host) over SSH, and TLS certificate
expiry. Alerts go to Telegram and/or a webhook, on state changes only.

It is NOT a monitoring stack. There is no daemon, no container, no time series
database and no dashboard: one Python script (standard library only) runs from
a systemd timer every 5 minutes on every swarm node, checks, alerts on what
changed, and exits.

## Resource cost

Per run, measured on a skstack06 lab node (see Testing):

| | |
|---|---|
| Processes between runs | none (a systemd timer only) |
| One run | about 0.3 to 2 s wall time, under 25 MB RSS, a few `docker`/`systemctl` calls |
| Disk | one script, a JSON config, a state file of a few KB per node |
| Network | the alert POSTs only (and the SSH/TLS checks on the primary) |
| Caps | the unit runs with `Nice=10`, `IOSchedulingClass=idle`, `CPUQuota=25%`, `MemoryMax=128M`, `TimeoutStartSec=4min` |

Compare the Prometheus/Grafana stack (`skmon`), which keeps several
containers and a TSDB resident on the cluster around the clock. skhealth does
not replace it for metrics or graphs; it answers "is something broken right
now" and tells you.

What it costs the disks: `smartctl` is run with `-n standby`, so a sleeping
disk is skipped instead of spun up. The swarm queries run on one node only
(the primary, below).

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skhealth/deploy_skhealth-prod.yml \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

The play targets `swarm_managers:swarm_workers` (every node). Override with
`-e skhealth_hosts=<group or pattern>`. On each host it installs:

| Path | Mode | What |
|---|---|---|
| `/usr/local/sbin/skhealth` | 0755 | the engine (`src/skhealth/skhealth`) |
| `/etc/skhealth/skhealth.json` | 0600 | runtime config (no secrets) |
| `/etc/skhealth/secrets.env` | 0600 | Telegram token and chat id, webhook URL: only the lines that are set |
| `/etc/skhealth/remote_ed25519`, `known_hosts` | 0600 | only when remote hosts are configured |
| `/etc/systemd/system/skhealth.{service,timer}` | 0644 | the oneshot unit and its timer |
| `/var/lib/skhealth/state.json` | 0600 | dedupe state (written by the engine) |

and `smartmontools` (apt) unless `disks.install_smartmontools` is false. The
deploy ends with `skhealth check` on every host, which prints the current
problems without alerting.

Commands on a host (as root):

```bash
skhealth --test     # send one test message through every configured channel
skhealth check      # print current problems; no alert, no state change
skhealth status     # saved state and which channels are configured (booleans)
skhealth run        # what the timer runs
journalctl -u skhealth.service -n 50
```

Every external command is called by absolute path and the engine sets its
own `PATH`: a timer's environment has a minimal `PATH`, and a script that
works from a shell but not from its scheduler is the failure this avoids.

## Alerting and dedupe

Each message names the cluster and the host: `[skhealth] <cluster> / <host>`,
then one line per change:

- `PROBLEM`: a problem seen on `confirm_runs` consecutive runs (default 2,
  so a service restarting during a deploy does not alert);
- `STILL FAILING`: the same problem again every `realert_hours` (default 6,
  0 = never) while it persists;
- `RECOVERED`: once, when an alerted problem clears (a blip that never
  alerted clears silently);
- `EVENT`: an OOM kill since the last run (alerted at once, never
  "recovered").

All changes of one run go out as one message per channel. The webhook gets
JSON: `{"source": "skhealth", "cluster", "host", "text", "events": [{"kind",
"key", "message"}]}`. If every configured channel fails, the state is not
saved, so the same changes are sent on the next run. Without any channel, the
changes only go to the journal.

Secrets are read from the root-only `secrets.env` and never printed: send
errors say `telegram: HTTP 401` or `webhook: URLError`, never a URL or token,
and `skhealth status` shows the channels as true/false.

## Checks

| Check | Runs on | Problem when |
|---|---|---|
| swarm services | primary | `running < desired` replicas (marked "stuck at 0" at 0); more than `crashloop_failed_tasks` failed or rejected tasks in `crashloop_minutes` (a crash loop) |
| swarm nodes | primary | a node not `Ready` or not `Active`, a manager `Unreachable` |
| filesystems | every host | `fs_paths` (and every NFS mount, with `nfs_usage`) at or over `fs_max_pct` |
| NFS mounts | every host | `timeout <nfs_timeout_seconds> stat -t <mount>` does not answer (a hung mount alerts; it is not statted again in the same run) |
| load / memory | every host | 5-minute load per CPU over `load_per_cpu_max`; `MemAvailable` under `mem_available_min_pct`; PSI `full avg60` over `mem_psi_full_avg60_max` |
| OOM | every host | an OOM-killer line in the kernel log since the last run |
| systemd | every host | any failed unit (minus `ignore_units` globs) |
| disks | every host | `smartctl -H` not PASSED/OK; NVMe critical warning non-zero; NVMe percentage used at or over `nvme_used_max_pct`. Skipped where smartctl is absent |
| ZFS / md RAID | every host | `zpool status -x` lists a pool (DEGRADED, FAULTED, ...); `mdadm --detail` says degraded/failed/inactive or counts failed devices. Skipped where neither exists |
| remote hosts | primary | the same ZFS/SMART rules, plus `pvesm status` storages not active or over `fs_max_pct`, on each configured remote host; SSH failure is its own problem |
| TLS | primary | certificate of `host[:port]` expires within `warn_days` (or cannot be read) |

**The primary.** The cluster-wide checks (services, nodes, remote hosts, TLS)
run on one host only, so each problem alerts once. `primary: auto` (default)
picks the Ready manager with the lowest node id, recomputed every run: when
it goes down, the next lowest takes over at the next run. Or name a host
(`primary: <short hostname>`). A host that is not the primary keeps whatever
it had saved for those checks without reporting them recovered.

A check that cannot run (tool missing, not the primary) keeps its saved
problems as they are. A check that crashes alerts as `check <name> failed`
and the other checks still run.

## Remote read-only host checks

For a storage host that is not a swarm node (the NAS or Proxmox host serving
NFS), skhealth logs in with a dedicated key and runs ONLY:

```
zpool status -x
smartctl -H <device>      smartctl -A <device>     (listed devices only)
pvesm status
```

**The deploy installs nothing on a remote host.** On each remote host an
operator installs the forced-command wrapper `src/skhealth/skhealth-remote`
from this directory and adds one `authorized_keys` line. The wrapper runs the
command only if `SSH_ORIGINAL_COMMAND` is exactly one of the four forms above
and the device is one of its arguments; anything else (other commands, extra
words, unlisted devices, shell metacharacters, an interactive login) is
denied with exit 126. Nothing goes through a shell.

1. Generate the key pair on the operator machine and put the private key in
   the vault (`remote.ssh_private_key`):
   `ssh-keygen -t ed25519 -N '' -C skhealth@<cluster> -f skhealth_ed25519`
2. On the remote host (as root):
   `install -o root -g root -m 0755 skhealth-remote /usr/local/sbin/skhealth-remote`
3. Add the public key with the forced command and `restrict` (no pty, no
   forwarding), listing the devices it may query:

   ```
   command="/usr/local/sbin/skhealth-remote /dev/nvme0n1 /dev/sda",restrict ssh-ed25519 AAAA... skhealth@<cluster>
   ```

   Either in root's `authorized_keys`, or (preferred) for a dedicated user
   such as `skhealth` with exactly these sudoers lines
   (`/etc/sudoers.d/skhealth`, mode 0440); the wrapper uses `sudo -n` when it
   is not root:

   ```
   skhealth ALL=(root) NOPASSWD: /usr/sbin/zpool status -x, /usr/sbin/pvesm status, /usr/sbin/smartctl -H /dev/*, /usr/sbin/smartctl -A /dev/*
   ```
4. Put the remote host's key line in `remote.known_hosts`
   (`ssh-keyscan -p <port> <host>`), so the check runs with
   `StrictHostKeyChecking=yes`. Without it the first connection is trusted
   (`accept-new`).
5. Test from the primary: `skhealth check`.

## Configuration (vault)

The `skhealth:` block of the instance vault is merged over
`vars/defaults.yml`, so set only what differs. Every key, with its default:

```yaml
skhealth:
  enabled: true                 # false: stop and remove the timer and units
  cluster_label: ""             # name in messages; empty = inventory cluster_name
  primary: auto                 # or a host's short hostname
  schedule: "*:0/5"             # systemd OnCalendar
  confirm_runs: 2
  realert_hours: 6
  alerts:
    telegram_bot_token: ""      # with telegram_chat_id: both or neither
    telegram_chat_id: ""
    telegram_api_base: "https://api.telegram.org"
    webhook_url: ""             # gets the JSON payload above
  swarm:
    enabled: true
    crashloop_failed_tasks: 3   # 0 = no crash-loop check
    crashloop_minutes: 15
    ignore_services: []         # regexes on service names
  nodes:
    enabled: true
    ignore_nodes: []
  host:
    enabled: true
    fs_paths: ["/"]
    fs_max_pct: 85              # also the pvesm storage limit
    nfs: true
    nfs_usage: true
    nfs_timeout_seconds: 5
    load_per_cpu_max: 2.0       # 0 = off
    mem_available_min_pct: 10   # 0 = off
    mem_psi_full_avg60_max: 10  # 0 = off
    oom: true
    failed_units: true
    ignore_units: []            # globs
  disks:
    enabled: true
    install_smartmontools: true
    nvme_used_max_pct: 90
  raid:
    enabled: true
  remote:
    hosts: []                   # [{name, host, port: 22, user: skhealth, zpool: false, smart_devices: [], pvesm: false}]
    ssh_private_key: ""         # required when hosts is set
    known_hosts: []
  tls:
    hosts: []                   # ["host", "host:port", {host, port, address}]
    warn_days: 21
```

`telegram_api_base`, `webhook_url`, the remote key and `known_hosts` are only
rendered when set; an unset block writes nothing.

## Testing

`v1/tests/test_skhealth_checks.py` (every check against stub commands that
record their arguments), `test_skhealth_alerts.py` (dedupe/recovery
transitions, end to end against a fake Telegram API and webhook),
`test_skhealth_remote_wrapper.py` (the wrapper allowlist) and
`test_skhealth_render.py` (the deploy through real Ansible). The skstack06
release harness deploys it on a lab cluster, runs `--test` against a local
fake webhook and proves that one injected failure gives exactly one alert and
one recovery.
