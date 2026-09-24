"""The real skmedia pack must resolve its full *arr wiring web + flag HA + rotation."""
from __future__ import annotations

from skwire import build_plan, redundancy_advice, rotation_schedule
from skwire.packs.skmedia import pack


def test_pack_resolves_the_full_arr_graph():
    nodes = pack().nodes
    plan = build_plan(nodes)
    o = plan.order
    # providers before consumers across the whole chain
    assert o.index("prowlarr") < o.index("sonarr") < o.index("seerr")
    assert o.index("qbittorrent") < o.index("sonarr")
    assert o.index("jellyfin") < o.index("seerr")
    # the dense API-key web is all minted
    assert {"prowlarr_api_key", "sonarr_api_key", "radarr_api_key",
            "jellyfin_api_key", "qbit_password"} <= plan.mints


def test_prowlarr_is_load_bearing_so_mantra_fires():
    nodes = pack().nodes
    advice = redundancy_advice(build_plan(nodes), nodes=nodes)
    # prowlarr is needed by sonarr AND radarr → flagged for a redundant pair
    assert any("prowlarr" in a.text for a in advice)


def test_rotation_policy_is_read_from_the_pack():
    sched = rotation_schedule(pack().nodes)
    assert sched["prowlarr_api_key"] == 90 and sched["jellyfin_api_key"] == 180


def test_pack_ships_a_real_injector_and_questions():
    p = pack()
    assert "servarr-api" in p.injectors
    assert p.injectors["servarr-api"].method == "api"
    assert any("jellyfin" in q.lower() or "plex" in q.lower() for q in p.questions)
