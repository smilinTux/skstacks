"""skhome + skdev packs resolve their wiring (MQTT spine; agent→endpoint)."""
from __future__ import annotations

from skwire import build_plan, redundancy_advice
from skwire.packs.skhome import pack as skhome
from skwire.packs.skdev import pack as skdev


def test_skhome_wires_around_the_mosquitto_spine():
    nodes = skhome().nodes
    plan = build_plan(nodes)
    assert {"mqtt_password", "frigate_api_key", "ha_long_lived_token"} <= plan.mints
    # mosquitto is the spine (critical + high fan-in) → mantra fires
    assert any("mosquitto" in a.text for a in redundancy_advice(plan, nodes=nodes))
    o = plan.order
    assert o.index("mosquitto") < o.index("frigate") < o.index("homeassistant")


def test_skdev_wires_agents_to_model_endpoints():
    nodes = skdev().nodes
    plan = build_plan(nodes)
    assert "openrouter_api_key" in plan.mints
    o = plan.order
    assert o.index("openrouter") < o.index("opencode")
    # openrouter is load-bearing (every agent needs it)
    assert any("openrouter" in a.text for a in redundancy_advice(plan, nodes=nodes))
