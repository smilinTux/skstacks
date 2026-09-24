"""Rotation scheduling — policy from descriptors, what's due, and the offer."""
from __future__ import annotations

from skwire.rotation import (
    rotation_schedule, due_for_rotation, cron_for, offer_rotation_schedule,
)


_NODES = [
    {"name": "garage", "provides": {"url": "x"},
     "secrets": [
         {"key": "garage_rpc_secret", "rotation_days": 365},
         {"key": "garage_admin_token", "rotation_days": 90},
         {"key": "garage_s3_access_key"},          # no rotation policy → skipped
     ]},
]


def test_schedule_read_from_descriptor_rotation_days():
    sched = rotation_schedule(_NODES)
    assert sched == {"garage_rpc_secret": 365, "garage_admin_token": 90}


def test_due_for_rotation_flags_overdue_keys():
    sched = rotation_schedule(_NODES)
    # admin_token last rotated day 0, now day 100 → 100 >= 90 → due; rpc not yet
    due = due_for_rotation(sched, {"garage_rpc_secret": 0, "garage_admin_token": 0}, now_days=100)
    assert due == ["garage_admin_token"]


def test_cron_for_days():
    assert cron_for(7) == "0 3 */7 * *"        # weekly-ish
    assert cron_for(90).startswith("0 3 1 */") # ~quarterly


def test_offer_asks_to_schedule_with_crons():
    offer = offer_rotation_schedule(_NODES)
    assert "want me to schedule" in offer.message.lower()
    assert offer.crons["garage_admin_token"] == cron_for(90)
    assert "garage_admin_token every 90d" in offer.message
