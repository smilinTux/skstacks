# Third-party icons: homarr-labs/dashboard-icons

Several roles ship a real service logo (`icon.png` / `icons/<name>.png`
under their own `src/config/<app>/`) instead of a Font Awesome glyph, for
use as the Authentik portal tile's `meta_icon` (see
`shared/tasks/authentik_portal_tile.yml`, `portal_tile.icon_file`).

These PNGs are taken as-is from the `png/` directory of:

- Project: [homarr-labs/dashboard-icons](https://github.com/homarr-labs/dashboard-icons)
- License: Apache License 2.0 (full text: `LICENSE` next to this file,
  fetched from the same repository)
- Retrieved: 2026-10-24, commit `d3b9234dd56f3af5862f06741f0dce101193b3df`
- No files were modified; each PNG is used unchanged under its original
  filename (minus path), as permitted by Apache-2.0 Section 4 (this NOTICE
  constitutes the attribution and license text Section 4(d) asks a
  redistributor to carry).

## Files and their roles

| Icon file (upstream `png/<name>.png`) | Shipped at | Role(s) |
|---|---|---|
| `alertmanager.png` | `optional/skmon/src/config/skmon/icons/alertmanager.png` | skmon (Alertmanager tile) |
| `bookstack.png` | `optional/skbook/src/config/skbook/icon.png` | skbook |
| `forgejo.png` | `optional/skgit/src/config/skgit/icon.png` | skgit |
| `garage.png` | `optional/skstor/src/config/skstor/icon.png` | skstor |
| `grafana.png` | `optional/skmon/src/config/skmon/icons/grafana.png` | skmon (Grafana tile) |
| `immich.png` | `optional/skgallery/src/config/skgallery/icon.png` | skgallery |
| `jaeger.png` | `optional/skmon/src/config/skmon/icons/jaeger.png` | skmon (Jaeger tile) |
| `lidarr.png` | `optional/skfetch/src/config/skfetch/icons/lidarr.png` | skfetch (Lidarr tile) |
| `meshcentral.png` | `optional/skrac/src/config/skrac/icon.png` | skrac |
| `n8n.png` | `optional/skorch/src/config/skorch/icon.png` | skorch |
| `netbird.png` | `optional/skmesh/src/config/skmesh/icon.png` | skmesh |
| `nextcloud.png` | `optional/skhub/src/config/skhub/icon.png` | skhub |
| `plex.png` | `optional/skstream/src/config/skstream/icon.png` | skstream |
| `portainer.png` | `optional/skport/src/config/skport/icon.png` | skport |
| `prometheus.png` | `optional/skmon/src/config/skmon/icons/prometheus.png` | skmon (Prometheus tile) |
| `prowlarr.png` | `optional/skfetch/src/config/skfetch/icons/prowlarr.png` | skfetch (Prowlarr tile) |
| `qdrant.png` | `optional/skvector/src/config/skvector/icon.png` | skvector |
| `radarr.png` | `optional/skfetch/src/config/skfetch/icons/radarr.png` | skfetch (Radarr tile) |
| `searxng.png` | `optional/skpeek/src/config/skpeek/icon.png` | skpeek |
| `sonarr.png` | `optional/skfetch/src/config/skfetch/icons/sonarr.png` | skfetch (Sonarr tile) |
| `stirling-pdf.png` | `optional/skpdf/src/config/skpdf/icon.png` | skpdf |
| `syncthing.png` | `optional/sksync/src/config/sksync/icon.png` | sksync |
| `traefik.png` | `core/skfence/src/config/skfence/icon.png`, `core/skfenceha/src/config/skfenceha/icon.png` | skfence, skfenceha |
| `uptime-kuma.png` | `optional/skpulse/src/config/skpulse/icon.png` | skpulse |
| `windmill.png` | `optional/skflow/src/config/skflow/icon.png` | skflow |

Not included (no checked entry in the upstream catalog for this exact
service, or it already renders fine with a Font Awesome glyph): skboard,
skdash, skgym, skhome, skmail, skpatch, skpayment, sksec-web, skseek,
skwhoami. Those roles keep their `fa://fa-...` fallback icon; a real logo
for any of them can be added the same way later if the project adds one.

`proxmox.png` was fetched for reference (it is one of the icon names
already uploaded by hand on the live estates) but is not shipped by any
role here: no `skstacks` role manages Proxmox itself, that tile's icon is
set directly on the instance, outside this repository.
