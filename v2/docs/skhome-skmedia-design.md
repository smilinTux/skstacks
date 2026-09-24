> Part of the SKStacks v2 stack — see [STACK-MAP.md](STACK-MAP.md) for the full architecture map + how it all connects.

I have enough to match both descriptor conventions (the lean `skos/apps` capability style and the richer SKStacks v2 `secrets[]`/`depends_on`/`config`/`healthcheck` style). The dossier is comprehensive and pre-cited. Writing the spec now.

---

# skhome + skmedia — Concrete Design Spec (SKStacks v2 / skos)
*Lead architect spec, mid-2026. All external facts cited from the research dossier; URLs inline.*

## 0. Framing & the one idea that ships this

Two net-new SKStacks v2 ports — **skhome** (home automation) and **skmedia** (media management) — each a set of ports/adapters whose interconnects are pure `URL:port + secret + depends_on`. Both are made *one-click* by a single new skos kernel subsystem, **`skos wire`**, built on **mint-then-inject**: skos generates every API key/secret *first*, injects known values at first boot, then pushes cross-service config over each app's config API. This inverts the historic "boot → scrape random key → paste everywhere" hell. It is now possible because (a) Servarr v4 supports `APP__SECTION__OPTION` env override (so `SONARR__AUTH__APIKEY` can be pre-seeded — supersedes the old "not planned" [Sonarr#5322](https://github.com/Sonarr/Sonarr/issues/5322), see [Servarr env-vars](https://wiki.servarr.com/sonarr/environment-variables)) and (b) qBittorrent ≥ v5.2.0 added pre-mintable [API-key auth](https://github.com/qbittorrent/qBittorrent/wiki/API-Key-Authentication-(%E2%89%A5v5.2.0)).

**Legal/ethical guardrail (baked into the install prompt + descriptors):** these stacks are for self-hosting cameras/devices and **media you own or have the right to**. The downloader defaults to VPN-gated egress (gluetun) as a *privacy* measure; piracy is not endorsed; descriptors default to legal sources and surface a VPN/privacy advisory.

---

## 1. skhome — Home Automation Port

**Hub model:** Home Assistant is the brain; **one shared Mosquitto MQTT broker is the spine** — Frigate, Zigbee2MQTT, Z-Wave JS UI, (optional) Double-Take all publish to it and HA consumes. Auto-wiring everything to one broker URL is the single highest-leverage move.

**Base decision:** use **HA Container** (`ghcr.io/home-assistant/home-assistant:stable`, :8123, `/config` vol), *not* HA OS — each capability stays an independent swappable skos adapter rather than an in-HA Add-on. Core/Supervised are deprecated as of 2025.12 ([HA deprecation blog](https://www.home-assistant.io/blog/2025/05/22/deprecating-core-and-supervised-installation-methods-and-32-bit-systems/), [HA OS vs Container](https://www.home-assistant.io/faq/ha-vs-hassio/)).

### 1.1 Adapter set

| Adapter | Image | Port(s) | Role |
|---|---|---|---|
| **homeassistant** (hub) | `ghcr.io/home-assistant/home-assistant:stable` | 8123 | automation engine, registry, dashboards, REST/WS API |
| **mosquitto** (spine) | `eclipse-mosquitto` | 1883 / 8883 / 9001 | shared MQTT broker |
| **frigate** (NVR) | `ghcr.io/blakeblackshear/frigate:stable` (+`-tensorrt`/`-rocm` accel tags) | 8971 (auth UI/API), 8554 (RTSP), 8555 (WebRTC), 1984 (go2rtc); **never expose 5000** | local AI NVR — object detect + native face/LPR/CLIP search (0.16+) |
| **zigbee2mqtt** | `koenkk/zigbee2mqtt` | 8080 | Zigbee radio → MQTT bridge (+USB coordinator) |
| **zwave-js-ui** | `zwavejs/zwave-js-ui` | 8091 (UI), 3000 (WS → HA `zwave_js`) | Z-Wave radio → HA (+USB stick) |
| **esphome** | `ghcr.io/esphome/esphome` | 6052 | build/flash DIY ESP32 firmware |
| **node-red** | `nodered/node-red` | 1880 | visual flow automation (advanced) |
| **double-take** *(opt)* | `skrashevich/double-take` (maintained fork; original `jakowenko` stale) | 3000 | face-rec aggregator over Frigate MQTT |
| **compreface** *(opt)* | `exadel/compreface` (multi-container) | 8000 (UI) | face-rec engine behind double-take |

### 1.2 Interconnect graph (what wires to what + which secret)

```
 cameras ──RTSP(cam_url+cam_creds)──► frigate ──pub──► mosquitto ◄──sub── homeassistant
                                         ▲                 ▲   ▲   ▲
                          FRIGATE_URL=http://frigate:8971  │   │   │
        homeassistant ──(Frigate HACS integ.)─────────────┘   │   │
        zigbee2mqtt ──mqtt://mosquitto:1883 (user/pass)───────┘   │
        zwave-js-ui ──(opt mqtt)──┘  └──WS :3000──► homeassistant  │
        node-red ──(HA long-lived token)──► homeassistant          │
        double-take ──FRIGATE_URL + mqtt + COMPREFACE_URL+key──────┘
```

Core edges + secret on each:
1. **cameras → frigate** — `RTSP_URL` + `cam_user`/`cam_pass`; go2rtc restreams (decode once, fan out).
2. **frigate → mosquitto** — `mqtt://mosquitto:1883` + `mqtt_user`/`mqtt_pass`. **MQTT is *required* for the HA integration; frigate and HA must point at the same broker.**
3. **homeassistant → frigate** (Frigate HACS integration) — reads Frigate's MQTT topics + calls `http://frigate:8971` (+ creds if auth) to mint camera/sensor entities, clips, media browser.
4. **node-red / double-take → homeassistant** — `HA long-lived access token` + target URL.
5. **zwave-js-ui → homeassistant** — WebSocket on :3000 (`zwave_js` integration), S2/S0 network key secret.

**Critical OOTB gotcha — Mosquitto 2.0+ defaults to anonymous-disabled / local-only.** skos MUST auto-render `mosquitto.conf` with `listener 1883` + a `mosquitto_passwd` file holding a minted service account, or every client throws auth-failure/`ECONNREFUSED` ([eclipse-mosquitto](https://hub.docker.com/_/eclipse-mosquitto), [Mosquitto+HA config](https://www.homeautomationguy.io/blog/docker-tips/configuring-the-mosquitto-mqtt-docker-container-for-use-with-home-assistant)).

### 1.3 Minimal great hobbyist bundle (3 containers)
**homeassistant + mosquitto + frigate** + the Frigate HACS integration. Delivers local AI camera detection, recordings, **native face-rec + LPR + CLIP semantic search (Frigate 0.16+, no Frigate+ sub)**, full HA automation/notifications, zero cloud. Cleanest default accel = **Intel OpenVINO on iGPU/Arc/NPU** (2026 default; Coral now only for low-power builds — [Frigate hardware](https://docs.frigate.video/frigate/hardware/), [0.16 face/LPR](https://docs.frigate.video/configuration/face_recognition/)). Add-as-needed (all MQTT-wired): zigbee2mqtt, zwave-js-ui, esphome, node-red. Double-Take+CompreFace = power-user only (Frigate's native face-rec covers most).

---

## 2. skmedia — Media Management Port

**Default = Jellyfin** (GPL-v2, no account, free HW transcode incl. 4K HDR tone-map, **no remote-streaming paywall**). **Plex coexists** as a swappable adapter (v1 prod already runs Plex+Kometa+pdzurg; both index the same on-disk folders). Plex's 2025–26 [remote-streaming paywall](https://9to5mac.com/2025/11/27/plex-paywall-for-remote-streaming-now-being-enforced/) is exactly the vendor-cloud dependency the sovereign default avoids ([Jellyfin vs Plex 2026](https://jellywatch.app/blog/jellyfin-vs-plex-2026-comparison)).

**Key 2026 consolidation:** Overseerr (archived) → Jellyseerr → **unified into "Seerr"** (`github.com/seerr-team/seerr`, v3.3.0 2026-06-02; :5055; supports Jellyfin/Plex/Emby). Target **Seerr v3.x**, not Overseerr/Jellyseerr. **Readarr is RETIRED** — ship books only as an optional/experimental adapter (**Chaptarr** or LazyLibrarian), never Readarr ([Servarr Readarr](https://wiki.servarr.com/readarr)).

### 2.1 Adapter set + ports

| Adapter | Image / role | Port | Secret minted |
|---|---|---|---|
| **jellyfin** (default server) | `jellyfin/jellyfin` | 8096 / 8920 | API key |
| **plex** (coexist) | `plexinc/pms-docker` | 32400 | X-Plex-Token (claim-once) |
| **seerr** (requests) | `seerr-team/seerr` | 5055 | own + consumes server/arr keys |
| **prowlarr** (indexer hub) | `prowlarr` | 9696 | API key |
| **sonarr** (TV) | `sonarr` | 8989 | API key |
| **radarr** (movies) | `radarr` | 7878 | API key |
| **lidarr** (music) | `lidarr` | 8686 | API key |
| **bazarr** (subtitles) | `bazarr` | 6767 | consumes sonarr/radarr keys |
| **qbittorrent** (torrent) | `qbittorrent` ≥5.2 | 8080 *(published on gluetun)* | API key (≥5.2) |
| **sabnzbd** (usenet) | `sabnzbd` | 8080 | API key (nzbkey) |
| **gluetun** (VPN) | `qmcgaw/gluetun` | 8000 (control) | VPN provider creds / wg key |
| **flaresolverr** | `flaresolverr` | 8191 | none (tag-matched) |
| **kometa** *(Plex)* / **JFC** *(Jellyfin)* | collections/overlays | — | plex_token+TMDb / TMDb+server |
| **tautulli** *(Plex)* / **jellystat** *(Jellyfin)* | analytics | 8181 / 3000 | plex url+token / jf url+key |
| **chaptarr** *(opt/exp)* | books (Readarr replacement) | 8787 | API key |

### 2.2 FULL inter-service wiring map (every URL:port + API-key edge)

```
 user ─► seerr:5055 ──(server: jellyfin:8096 + JF_API_KEY  | plex:32400 + X-Plex-Token)
            │   ├──► radarr:7878  (RADARR_API_KEY) + quality/root/lang
            │   └──► sonarr:8989  (SONARR_API_KEY) + quality/root/lang
 prowlarr:9696 ──POST /api/v1/applications (fullSync)──► sonarr:8989  (PROWLARR_URL + SONARR_API_KEY)
            │                                          ► radarr:7878  (PROWLARR_URL + RADARR_API_KEY)
            │                                          ► lidarr:8686  (PROWLARR_URL + LIDARR_API_KEY)
            └──Indexer Proxy──► flaresolverr:8191 (tag=flaresolverr, only on CF-detect)
 sonarr/radarr/lidarr ──Download Clients──► gluetun:8080 (qBit API key, category)   ◄── NOT qbittorrent:8080
                       └──────────────────► sabnzbd:8080 (SAB_API_KEY, category) [clearnet, outside gluetun]
 bazarr:6767 ──► sonarr:8989 (SONARR_API_KEY) ; ──► radarr:7878 (RADARR_API_KEY)
 *arr ──import/hardlink──► /data shared vol ──► jellyfin:8096 / plex:32400 (lib scan + refresh hook)
 kometa ──► plex:32400 (X-Plex-Token + TMDb [+Trakt])        [JFC if Jellyfin: TMDb + jellyfin:8096]
 tautulli ──► plex:32400 (X-Plex-Token)                       [jellystat if Jellyfin: jellyfin:8096 + JF_API_KEY]
 qbittorrent ──network_mode: service:gluetun──► VPN tunnel (kill-switch implicit)
```

**The five silent-failure rules the auto-wirer encodes** (this is the ~15–20 manual connection "hell" it kills):
1. **gluetun namespace** — qBittorrent runs `network_mode: service:gluetun`; its WebUI (8080) is published *on gluetun*. Every *arr's download-client host = `http://gluetun:8080`, **not** `qbittorrent:8080` (the #1 hobbyist mistake). ([tatewalker](https://www.tatewalker.com/blog/docker-torrent/))
2. **Port collision in namespace** — qBit 8080 vs SAB 8080: usenet is clearnet, runs *outside* gluetun (no conflict).
3. **Namespace-rebind** — Docker doesn't auto-rebind on gluetun restart → encode `depends_on` + restart-follow ([gluetun #2686](https://github.com/qdm12/gluetun/discussions/2686)).
4. **VPN port-forward** — dynamic forwarded port; run a port-bind sidecar (`qbittorrent-gluetun-port-bind`) as a managed wire.
5. **Shared `/data`** — mount one identical `/data` across all containers so hardlinks work and remote-path-mapping is unnecessary ([TRaSH remote path](https://trash-guides.info/Sonarr/Tips/Sonarr-remote-path-mapping/)).

**Capability-routed substitution:** pick "Jellyfin" → Seerr+Jellystat+JFC; pick "Plex" → Kometa+Tautulli. Same `analytics`/`collections` capability ports, different adapter bound.

---

## 3. THE HEADLINE — `skos wire` OOTB Auto-Wiring Resolver

A new skos kernel subsystem that operates on the ports/adapters descriptor graph (your existing `secrets[]` / `depends_on` / config URL templates). Five phases.

### Phase A — Topology resolution (FQDN:port)
Topological sort over `depends_on`; assign each adapter a stable address from the **descriptor** (source of truth, not the running container). Two planes, both templated: **internal** (`{service}` compose DNS — used for all cross-service wiring to dodge TLS/proxy hairpinning) and **external** (`{service}.{stack}.{domain}` via skfence/Traefik for humans/SSO). Collision check → auto-bump → write resolved port back into the rendered descriptor instance. Output: `wiring-graph.json` with `{internal_url, external_url, port, health_endpoint}` per node.

### Phase B — Secret mint & inject (the lever)
For each `secrets[]` entry: **mint first** in the skos backend (`skstacks_secret_set` / capauth) — for *arr keys, generate 32-char hex matching Servarr format. Inject via the descriptor's declared `secret_injection.method`:
- `env_override` → Servarr (`SONARR__AUTH__APIKEY={{secret:...}}`), qBittorrent ≥5.2 API key, Jellyfin where supported.
- `config_file` → render templated config pre-boot (HA `secrets.yaml`, qBit <5.2 fallback).
- `api_post` → services that can only mint post-boot (Phase D).

Secrets never land in descriptor/compose as plaintext — only `{{secret:...}}` refs resolved at render time (dovetails with the leaked-creds discipline).

### Phase C — Cross-service config push (the actual auto-wiring)
Walk `depends_on` edges; for each, run a typed **idempotent connector** (GET-existing → diff → POST/PUT on drift, Buildarr semantics) behind a **readiness gate** (poll health endpoint to 200 before targeting). Connectors are declared in descriptors:

```yaml
wires:
  - to: prowlarr
    connector: prowlarr.add_application
    args: { app: sonarr, sync_level: fullSync }
```

| Edge | Confirmed mechanism |
|---|---|
| Prowlarr → Sonarr/Radarr/Lidarr | `POST /api/v1/applications` (URL+key, `fullSync`) → auto-pushes all indexers ([Prowlarr indexer-sync](https://deepwiki.com/Prowlarr/Prowlarr/4.1-indexer-sync-mechanism)) |
| *arr → qBittorrent | `POST /api/v3/downloadclient` (schema from `/schema`) host=`gluetun`+qBit key+category |
| *arr → root/quality | `POST /api/v3/rootfolder` then Phase-E formats |
| Seerr → Jellyfin + *arr | REST `:5055/api-docs` — set server+key, sync libs, add Radarr/Sonarr |
| Prowlarr → FlareSolverr | Indexer Proxy `http://flaresolverr:8191` + tag |
| HA ↔ integrations | post-bootstrap WS `auth/long_lived_access_token` + REST/WS ([HA Auth API](https://developers.home-assistant.io/docs/auth_api/)) |

### Phase D — Post-boot identity bootstrap (non-injectable services)
Flagged `bootstrap: onboarding`. One-shot drivers:
- **Home Assistant** — no env owner creds; drive `/api/onboarding/users` to create owner with minted creds, open WS, call `auth/long_lived_access_token` (10-yr), store token ([HA onboarding](https://www.home-assistant.io/getting-started/onboarding/)).
- **Jellyfin** — drive startup-wizard API → admin + library paths → mint API key for Seerr.
- **Plex** — claim-token scrape once → Kometa/Tautulli consume.

### Phase E — Opinionated content config (steal, don't build)
Vendor **Configarr ≥ v1.22.0** (handles post-Feb-2026 TRaSH JSON) as the quality engine — hand it resolved URLs+keys, it syncs TRaSH custom-formats + quality profiles into Sonarr/Radarr ([configarr.de](https://configarr.de/)). Recyclarr = fallback.

### Re-entrancy & prior art to steal

| Project | Take | URL |
|---|---|---|
| **Buildarr** | idempotent instance-linking + `depends_on` resolution = our connector model | [buildarr.github.io](https://buildarr.github.io/) |
| **Configarr** | Phase-E quality engine (vendored) | [configarr.de](https://configarr.de/) |
| **Recyclarr** | YAML template for quality-profile descriptor fields | [recyclarr.dev](https://recyclarr.dev/) |
| **Saltbox** | Ansible-role-with-shared-vars = our resolved `wiring-graph.json` as shared var-space | [Saltbox](https://github.com/saltyorg/Saltbox) |
| **devopsarr Terraform** | resource schemas (downloadclient/rootfolder/application) = ready-made connector payloads | [devopsarr/sonarr](https://registry.terraform.io/providers/devopsarr/sonarr/latest/docs) |

**Net-new vs prior art:** none of these mint secrets *first* + inject pre-boot, and none are LLM-driven from natural-language intent. skos's contribution: (a) mint-then-inject keyed off existing `secrets[]`, (b) one resolver graph unifying media + home automation, (c) the conversational profile generator. Connectors themselves = "Buildarr/Configarr re-homed as skos adapters" — buy, don't build.

Re-running is idempotent → "add Seerr later" is a one-command re-resolve.

---

## 4. AI-First / skos install flow + app-store tiles

**Conversational install (LLM over the resolver):**
1. **Elicit** — 3–4 questions: *"Media, home automation, or both?"* → *"Storage path / size?"* → *"Public (reverse proxy + SSO) or LAN-only?"* → *"VPN-gate downloads?"* LLM maps free-text → capability selections.
2. **Generate profile** — emits a skos profile (descriptor instances + adapter choices Jellyfin-vs-Plex / qBittorrent-vs-pdzurg + storage/network params), reviewed as a rendered dependency-graph plan (à la `superpowers:brainstorming` → `writing-plans`).
3. **Resolve + deploy** — one command: render → mint secrets → bring up containers (existing `run_ansible_playbook` / trustee path) → run resolver Phases A–E → health-gate.
4. **Verify** — run each connector's built-in test (`POST /api/v3/downloadclient/test`, Prowlarr "test application", Seerr "Test Connection") → green/red wiring matrix; LLM narrates failures + self-heals (re-mint, re-push).
5. **Result** — hobbyist lands on a working Seerr where requesting a movie flows Seerr→Radarr→(Prowlarr indexers)→qBittorrent(via gluetun)→Jellyfin, zero config screens touched.

VPN/privacy/legal one-liner is hardwired into the profile-generator system prompt.

**App-store tiles** (one per capability, skstacks-dashboard / skos.skworld.io install.sh):
- `skhome` tile → "Local AI home + cameras, zero cloud" → minimal bundle (HA+Mosquitto+Frigate); expand for Zigbee/Z-Wave/ESPHome.
- `skmedia` tile → "Sovereign media server, fully auto-wired" → Jellyfin default toggle ↔ Plex; checkbox sub-adapters (requests / subtitles / analytics / VPN). Each tile = a profile preset fed straight into step 2.

---

## 5. `app.yaml` descriptor sketches (matching SKStacks v2 style)

> Schema delta vs existing descriptors: add `secret_injection`, `wires`, `bootstrap`, `health_endpoint`. Everything else (`secrets[]`, `depends_on`, `config`, URL templates) already exists.

### skhome — `homeassistant/app.yaml`
```yaml
name: homeassistant
capability: home-hub
scope: skhome
description: "Home Assistant Container — automation hub, REST/WS API."
version: "2026.6"
platforms: [docker-swarm, kubernetes]
packaging:
  oci: { image: ghcr.io/home-assistant/home-assistant:stable, ports: [8123] }
data: [config]
bootstrap: onboarding            # no env owner creds; drive /api/onboarding/users
secrets:
  - key: ha_owner_user      {required: true}
  - key: ha_owner_password  {required: true, sensitive: true}
  - key: ha_long_lived_token  # minted post-onboarding via WS auth/long_lived_access_token
secret_injection: { method: api_post }
health_endpoint: "http://homeassistant:8123/api/"
depends_on: [mosquitto]
config: { TZ: "America/New_York" }
```

### skhome — `mosquitto/app.yaml`
```yaml
name: mosquitto
capability: mqtt-broker
scope: skhome
description: "Eclipse Mosquitto MQTT broker — skhome message spine."
packaging:
  oci: { image: eclipse-mosquitto, ports: [1883, 8883, 9001] }
data: [config, data, log]
secrets:
  - key: mqtt_user      {required: true, default: skhome}
  - key: mqtt_password  {required: true, sensitive: true}
# 2.0+ is anonymous-OFF by default: MUST render conf + passwd or all clients fail.
secret_injection:
  method: config_file
  render:
    - { path: /mosquitto/config/mosquitto.conf, template: "listener 1883\nallow_anonymous false\npassword_file /mosquitto/config/pwfile" }
    - { path: /mosquitto/config/pwfile, generator: "mosquitto_passwd {{secret:mqtt_user}} {{secret:mqtt_password}}" }
health_endpoint: "tcp://mosquitto:1883"
depends_on: []
required_by: [homeassistant, frigate, zigbee2mqtt, zwave-js-ui, double-take]
```

### skhome — `frigate/app.yaml`
```yaml
name: frigate
capability: nvr
scope: skhome
description: "Frigate NVR — local AI detect + native face/LPR/CLIP (0.16+)."
packaging:
  oci: { image: ghcr.io/blakeblackshear/frigate:stable, ports: [8971, 8554, 8555, 1984] }
  # accel tags: stable-tensorrt (NVIDIA) | stable-rocm (AMD) | stable (Intel OpenVINO/RPi/Hailo)
data: [config, media, cache]
devices: ["/dev/dri"]            # Intel iGPU/Arc default accel
shm_size: 256m
secrets:
  - key: frigate_mqtt_user      {from: mosquitto.mqtt_user}
  - key: frigate_mqtt_password  {from: mosquitto.mqtt_password, sensitive: true}
  - key: cam0_rtsp_url          {required: true}
  - key: cam0_user              {required: true}
  - key: cam0_password          {required: true, sensitive: true}
secret_injection: { method: config_file, render: [{ path: /config/config.yml, template: frigate.yml.j2 }] }
health_endpoint: "http://frigate:8971/api/version"
depends_on: [mosquitto]
config: { FRIGATE_URL: "http://frigate:8971" }
wires:
  - to: homeassistant            # Frigate HACS integration consumes MQTT topics + frigate API
    connector: homeassistant.add_frigate_integration
    args: { url: "http://frigate:8971" }
# expose ONLY 8971/8554/8555 behind skfence; NEVER expose 5000 (unauth).
```

### skmedia — `sonarr/app.yaml`
```yaml
name: sonarr
capability: tv-pvr
scope: skmedia
description: "Sonarr v4 — TV PVR. API-key pre-seeded via env override."
packaging: { oci: { image: lscr.io/linuxserver/sonarr:latest, ports: [8989] } }
data: [config]
mounts: [{ vol: media, path: /data }]   # one shared /data for hardlinks
secrets:
  - key: sonarr_apikey  {required: true, sensitive: true}   # minted FIRST (32-char hex)
secret_injection: { method: env_override, key_template: "SONARR__AUTH__APIKEY" }
health_endpoint: "http://sonarr:8989/api/v3/system/status"
depends_on: [prowlarr, qbittorrent, sabnzbd, jellyfin]
wires:
  - to: qbittorrent
    connector: sonarr.add_download_client     # POST /api/v3/downloadclient
    args: { host: gluetun, port: 8080, category: tv-sonarr, use_api_key: true }
  - to: sabnzbd
    connector: sonarr.add_download_client
    args: { host: sabnzbd, port: 8080, category: tv, clearnet: true }
config: { quality_engine: configarr }         # Phase-E TRaSH sync
```

### skmedia — `prowlarr/app.yaml`
```yaml
name: prowlarr
capability: indexer-hub
scope: skmedia
packaging: { oci: { image: lscr.io/linuxserver/prowlarr:latest, ports: [9696] } }
secrets:
  - key: prowlarr_apikey  {required: true, sensitive: true}
secret_injection: { method: env_override, key_template: "PROWLARR__AUTH__APIKEY" }
health_endpoint: "http://prowlarr:9696/api/v1/system/status"
depends_on: [sonarr, radarr, lidarr, flaresolverr]
wires:
  - { to: sonarr, connector: prowlarr.add_application, args: { sync_level: fullSync } }
  - { to: radarr, connector: prowlarr.add_application, args: { sync_level: fullSync } }
  - { to: lidarr, connector: prowlarr.add_application, args: { sync_level: fullSync } }
  - { to: flaresolverr, connector: prowlarr.add_indexer_proxy, args: { tag: flaresolverr } }
```

### skmedia — `qbittorrent/app.yaml` (the gluetun special case)
```yaml
name: qbittorrent
capability: download-torrent
scope: skmedia
packaging: { oci: { image: lscr.io/linuxserver/qbittorrent:latest, ports: [] } }  # no own ports
network_mode: "service:gluetun"        # shares VPN namespace; 8080 published ON gluetun
restart_follows: gluetun               # rebind gotcha: restart when gluetun restarts
secrets:
  - key: qbit_apikey  {required: true, sensitive: true}   # qBittorrent >=5.2 pre-mintable
secret_injection: { method: config_file, render: [{ path: /config/qBittorrent/qBittorrent.conf }] }
health_endpoint: "http://gluetun:8080/api/v2/app/version"   # NOTE: gluetun host, not qbittorrent
depends_on: [gluetun]
sidecars:
  - { name: port-bind, image: mhabes/gluetun-qbittorrent-port-bind }   # push VPN fwd-port into qBit
```

### skmedia — `seerr/app.yaml`
```yaml
name: seerr
capability: request-portal
scope: skmedia
description: "Seerr v3 — unified Overseerr/Jellyseerr successor (Jellyfin/Plex/Emby)."
packaging: { oci: { image: ghcr.io/seerr-team/seerr:latest, ports: [5055] } }
secrets:
  - key: seerr_apikey  {required: true, sensitive: true}
secret_injection: { method: config_file }     # set post-boot via REST :5055/api-docs
health_endpoint: "http://seerr:5055/api/v1/status"
depends_on: [jellyfin, radarr, sonarr]         # swap jellyfin->plex when server=plex
wires:
  - { to: jellyfin, connector: seerr.set_media_server, args: { url: "http://jellyfin:8096" } }
  - { to: radarr,   connector: seerr.add_service, args: { type: radarr } }
  - { to: sonarr,   connector: seerr.add_service, args: { type: sonarr } }
legal_note: "Self-host media you own/have rights to. Downloads default VPN-gated. Piracy not endorsed."
```

---

## 6. Build roadmap

**M0 — Descriptor schema delta (1 sprint).** Add `secret_injection`, `wires`, `bootstrap`, `health_endpoint`, `restart_follows`, `sidecars`, `from:` (secret-reference) to the SKStacks v2 descriptor schema + validator. Author all skhome/skmedia `app.yaml`s above. *Gate: schema validates, no plaintext secrets.*

**M1 — `skos wire` resolver, Phases A+B (2 sprints).** Topology sort + `wiring-graph.json`; secret mint (capauth/`skstacks_secret_set`) + the 3 injection methods (env_override, config_file, api_post). *Gate: stack boots with pre-minted known keys (no scrape).*

**M2 — Connectors, Phase C+D (3 sprints).** Re-home Buildarr/devopsarr payload schemas as skos connectors (prowlarr.add_application, *arr.add_download_client, seerr.*, ha.*). Readiness gate + idempotent diff/push. Bootstrap drivers for HA onboarding + Jellyfin wizard + Plex claim. *Gate: `request movie in Seerr` → Jellyfin, zero manual config.*

**M3 — Phase E + verify loop (1 sprint).** Vendor Configarr ≥1.22.0; wire built-in connector tests → green/red matrix → LLM self-heal. *Gate: TRaSH profiles applied; verify matrix all-green.*

**M4 — Swarm + K8s stacks (2 sprints, parallel).** Render resolver output to **docker-swarm** compose/stack files (incl. gluetun `service:` namespace + shared `/data` overlay vol) AND **kubernetes/rke2** manifests (gluetun as sidecar in same pod for namespace-share; PVC for `/data`; sealed-secret refs). Reuse skfence/Traefik for external plane + sksso. *Gate: identical wiring on both platforms.*

**M5 — AI-first install + app-store tiles (2 sprints).** Conversational profile generator (elicit→profile→plan-review→deploy→verify) on the resolver; skhome/skmedia tiles in skstacks-dashboard + skos.skworld.io install.sh presets. Bake VPN/privacy/legal guardrail into the system prompt. *Gate: one-sentence intent → fully-wired stack, end-to-end demo.*

**M6 — Hardening.** Accel-tag auto-detection (Frigate OpenVINO/TensorRT/ROCm), Mosquitto 2.0 conf generator hardening, gluetun port-forward sidecar, optional adapters (Double-Take/CompreFace, Chaptarr, Lidarr/JFC/Jellystat routing).

---

**Buy-don't-build summary:** connectors = Buildarr/Configarr/devopsarr re-homed; quality = Configarr vendored; shared-var pattern = Saltbox. skos's three genuinely net-new pieces: **mint-then-inject** secret ordering, **one resolver graph across media + home**, and the **conversational profile generator**. The descriptor schema delta is small (4 new fields) and everything else rides on the `secrets[]`/`depends_on`/config templates that already exist.

*Descriptor style matched against the two live conventions found in-repo: `~/clawd/research/skstacks/SKStacks/v2/core/skfence/app.yaml` (rich: secrets[]/config/healthcheck/depends_on/required_by) and `~/clawd/skos/apps/{skmemory,capauth}/app.yaml` (lean: name/capability/packaging.oci/data).*