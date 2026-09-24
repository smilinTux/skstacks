"""
skhome — Home Assistant + Frigate NVR + the Zigbee/Z-Wave radios, auto-wired around
one Mosquitto MQTT spine (the single highest-leverage connection — everything talks
to the same broker). MQTT is REQUIRED for the HA↔Frigate integration.
"""
from __future__ import annotations

from skwire import Pack


def pack() -> Pack:
    return Pack(
        name="skhome",
        nodes=[
            {"name": "mosquitto", "provides": {"url": "mqtt://mosquitto:1883", "api_kind": "mqtt"},
             "secrets": [{"key": "mqtt_password", "rotation_days": 180}], "critical": True},
            {"name": "frigate", "provides": {"url": "http://frigate:8971", "api_kind": "frigate"},
             "needs": [{"service": "mosquitto", "secret": "mqtt_password"}]},
            {"name": "zigbee2mqtt", "provides": {"url": "http://zigbee2mqtt:8080"},
             "needs": [{"service": "mosquitto", "secret": "mqtt_password"}]},
            {"name": "homeassistant", "provides": {"url": "http://homeassistant:8123", "api_kind": "homeassistant"},
             "secrets": [{"key": "ha_long_lived_token", "rotation_days": 365}],
             "needs": [{"service": "mosquitto", "secret": "mqtt_password"},
                       {"service": "frigate", "secret": "frigate_api_key"}]},
            {"name": "node-red", "provides": {"url": "http://node-red:1880"},
             "needs": [{"service": "homeassistant", "secret": "ha_long_lived_token"}]},
        ],
        questions=["Add a Zigbee or Z-Wave USB radio?",
                   "Camera GPU/NPU for Frigate detection (OpenVINO/Coral)?"],
    )
