"""
skmedia — the real media pack: Jellyfin + the *arr stack + qBittorrent + Seerr,
fully auto-wired. This is THE showcase for mint-then-inject: the dense web of
API-key + URL connections that's normally hours of manual config is just declared
here and wired in one `skwire up`.
"""
from __future__ import annotations

from skwire import Pack
from skwire.inject import ApiPostInjector


def pack() -> Pack:
    return Pack(
        name="skmedia",
        nodes=[
            {"name": "jellyfin", "provides": {"url": "http://jellyfin:8096", "api_kind": "jellyfin"},
             "secrets": [{"key": "jellyfin_api_key", "rotation_days": 180}]},
            {"name": "qbittorrent", "provides": {"url": "http://qbittorrent:8080", "api_kind": "qbittorrent"},
             "secrets": [{"key": "qbit_password", "rotation_days": 90}]},
            {"name": "prowlarr", "provides": {"url": "http://prowlarr:9696", "api_kind": "servarr"},
             "secrets": [{"key": "prowlarr_api_key", "rotation_days": 90}]},
            {"name": "sonarr", "provides": {"url": "http://sonarr:8989", "api_kind": "servarr"},
             "secrets": [{"key": "sonarr_api_key", "rotation_days": 90}],
             "needs": [{"service": "prowlarr", "secret": "prowlarr_api_key"},
                       {"service": "qbittorrent", "secret": "qbit_password"}]},
            {"name": "radarr", "provides": {"url": "http://radarr:7878", "api_kind": "servarr"},
             "secrets": [{"key": "radarr_api_key", "rotation_days": 90}],
             "needs": [{"service": "prowlarr", "secret": "prowlarr_api_key"},
                       {"service": "qbittorrent", "secret": "qbit_password"}]},
            {"name": "bazarr", "provides": {"url": "http://bazarr:6767"},
             "needs": [{"service": "sonarr", "secret": "sonarr_api_key"},
                       {"service": "radarr", "secret": "radarr_api_key"}]},
            {"name": "seerr", "provides": {"url": "http://seerr:5055"},
             "needs": [{"service": "jellyfin", "secret": "jellyfin_api_key"},
                       {"service": "sonarr", "secret": "sonarr_api_key"},
                       {"service": "radarr", "secret": "radarr_api_key"}]},
        ],
        injectors={"servarr-api": ApiPostInjector("http://{target}/api/v3/config")},
        questions=["Media server — Jellyfin (default, FOSS) or Plex?",
                   "VPN-gate the downloader with gluetun? (recommended)"],
    )
