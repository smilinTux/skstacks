# Copyright (C) 2026 S&K Holding QT (Quantum Technologies)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Unit tests for pve_sso_check.py - the Proxmox SSO parity/dry-run checker.

No live Proxmox host is required: every test feeds fixture JSON (the same
shape `pvesh get ... --output-format json` returns) straight into the pure
diff/plan functions. These are also the fixtures the ansible role's
check/dry-run mode exercises before ever touching a real host.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pve_sso_check as m  # noqa: E402


REALM_NAME = "authentik"

TARGET_REALM_KEYS = {
    "groups-claim": "groups",
    "groups-autocreate": 1,
    "groups-overwrite": 1,
}
TARGET_SCOPES = {"openid", "profile", "email", "groups"}
TARGET_GROUPS = ["skadmin-authentik", "skeditor-authentik", "skreadonly-authentik"]
TARGET_ACLS = [
    ("/", "Administrator", "skadmin-authentik"),
    ("/", "PVEVMAdmin", "skeditor-authentik"),
    ("/", "PVEAuditor", "skreadonly-authentik"),
]


# ---------------------------------------------------------------------------
# diff_realm
# ---------------------------------------------------------------------------

def test_diff_realm_reports_all_missing_on_a_bare_realm():
    current = {
        "type": "openid",
        "username-claim": "username",
        "autocreate": 1,
        "scopes": "email profile",
    }
    diff = m.diff_realm(current, TARGET_REALM_KEYS, TARGET_SCOPES)
    assert diff["missing_keys"] == {
        "groups-claim": "groups",
        "groups-autocreate": 1,
        "groups-overwrite": 1,
    }
    # pveum's own default scopes ("email profile") omits "openid" too.
    assert diff["missing_scopes"] == {"groups", "openid"}
    assert diff["aligned"] is False


def test_diff_realm_clean_when_already_aligned():
    current = {
        "type": "openid",
        "username-claim": "subject",
        "groups-claim": "groups",
        "groups-autocreate": 1,
        "groups-overwrite": 1,
        "scopes": "openid profile email groups",
    }
    diff = m.diff_realm(current, TARGET_REALM_KEYS, TARGET_SCOPES)
    assert diff["missing_keys"] == {}
    assert diff["missing_scopes"] == set()
    assert diff["aligned"] is True


def test_diff_realm_never_reports_username_claim_as_drift():
    # username-claim is explicitly per-host and must never be "corrected".
    current = {
        "type": "openid",
        "username-claim": "username",  # differs from another host's "subject"
        "groups-claim": "groups",
        "groups-autocreate": 1,
        "groups-overwrite": 1,
        "scopes": "openid profile email groups",
    }
    diff = m.diff_realm(current, TARGET_REALM_KEYS, TARGET_SCOPES)
    assert diff["aligned"] is True
    assert "username-claim" not in diff["missing_keys"]


def test_diff_realm_accepts_superset_scopes():
    current = {
        "groups-claim": "groups",
        "groups-autocreate": 1,
        "groups-overwrite": 1,
        "scopes": "openid profile email groups extra-scope",
    }
    diff = m.diff_realm(current, TARGET_REALM_KEYS, TARGET_SCOPES)
    assert diff["missing_scopes"] == set()
    assert diff["aligned"] is True


# ---------------------------------------------------------------------------
# diff_groups
# ---------------------------------------------------------------------------

def test_diff_groups_reports_missing_only():
    current = ["skadmin", "skeditor", "skreadonly", "skadmin-authentik"]
    missing = m.diff_groups(current, TARGET_GROUPS)
    assert missing == ["skeditor-authentik", "skreadonly-authentik"]


def test_diff_groups_empty_when_all_present():
    missing = m.diff_groups(TARGET_GROUPS, TARGET_GROUPS)
    assert missing == []


def test_normalize_groups_accepts_raw_pvesh_output():
    # `pvesh get /access/groups --output-format json` returns a list of
    # dicts, not bare group-id strings - the CLI path must handle both.
    raw = [{"groupid": "skadmin", "comment": "admin account"}, {"groupid": "skeditor-authentik"}]
    assert m.normalize_groups(raw) == ["skadmin", "skeditor-authentik"]


def test_normalize_groups_passes_through_plain_strings():
    assert m.normalize_groups(["skadmin", "skeditor-authentik"]) == ["skadmin", "skeditor-authentik"]


# ---------------------------------------------------------------------------
# diff_acls
# ---------------------------------------------------------------------------

def test_diff_acls_reports_missing_grants():
    current = [
        {"path": "/", "roleid": "skadmin", "type": "group", "ugid": "skadmin"},
        {"path": "/", "roleid": "skadmin", "type": "group", "ugid": "skadmin-authentik"},
    ]
    missing = m.diff_acls(current, TARGET_ACLS)
    assert missing == [
        ("/", "Administrator", "skadmin-authentik"),
        ("/", "PVEVMAdmin", "skeditor-authentik"),
        ("/", "PVEAuditor", "skreadonly-authentik"),
    ]


def test_diff_acls_ignores_extra_legacy_grants():
    # The legacy manual-group ACLs must never be treated as drift - they are
    # retired by a separate, explicitly-confirmed step, not by this checker.
    current = [
        {"path": "/", "roleid": "Administrator", "type": "group", "ugid": "skadmin"},
        {"path": "/", "roleid": "Administrator", "type": "group", "ugid": "skadmin-authentik"},
        {"path": "/", "roleid": "PVEVMAdmin", "type": "group", "ugid": "skeditor-authentik"},
        {"path": "/", "roleid": "PVEAuditor", "type": "group", "ugid": "skreadonly-authentik"},
    ]
    missing = m.diff_acls(current, TARGET_ACLS)
    assert missing == []


# ---------------------------------------------------------------------------
# plan_commands - the dry-run / check-mode output
# ---------------------------------------------------------------------------

def test_plan_commands_empty_when_fully_aligned():
    plan = m.plan_commands(
        realm_name=REALM_NAME,
        realm_diff={"missing_keys": {}, "missing_scopes": set(), "aligned": True},
        missing_groups=[],
        missing_acls=[],
    )
    assert plan == []


def test_plan_commands_never_emits_username_claim():
    plan = m.plan_commands(
        realm_name=REALM_NAME,
        realm_diff={
            "missing_keys": {"groups-claim": "groups"},
            "missing_scopes": {"groups"},
            "aligned": False,
        },
        missing_groups=["skeditor-authentik"],
        missing_acls=[("/", "PVEVMAdmin", "skeditor-authentik")],
    )
    joined = " ".join(plan)
    assert "username-claim" not in joined
    assert "pveum realm modify authentik --groups-claim groups" in plan[0]
    assert any(cmd.startswith("pveum group add skeditor-authentik") for cmd in plan)
    assert "pveum acl modify / --roles PVEVMAdmin --groups skeditor-authentik" in plan


def test_plan_commands_merges_scopes_with_existing_ones_on_realm_modify():
    plan = m.plan_commands(
        realm_name=REALM_NAME,
        realm_diff={
            "missing_keys": {},
            "missing_scopes": {"groups"},
            "aligned": False,
        },
        missing_groups=[],
        missing_acls=[],
        current_scopes={"openid", "profile", "email"},
    )
    assert len(plan) == 1
    # order-independent scope check
    cmd = plan[0]
    assert cmd.startswith("pveum realm modify authentik --scopes '")
    scopes_in_cmd = set(cmd.split("'")[1].split())
    assert scopes_in_cmd == {"openid", "profile", "email", "groups"}


# ---------------------------------------------------------------------------
# exit-code contract (what a parity harness like live_parity relies on)
# ---------------------------------------------------------------------------

def test_exit_code_for_aligned_state_is_zero():
    assert m.exit_code_for(aligned=True) == 0


def test_exit_code_for_drift_is_one():
    assert m.exit_code_for(aligned=False) == 1
