#!/usr/bin/env python3
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
"""pve_sso_check.py - idempotent Proxmox SSO parity / dry-run checker.

Compares one Proxmox VE host's live `authentik` realm + ACLs against the
SKStacks standard (see ../README.md), and either:
  - reports the drift and exits non-zero (--check, the default), or
  - emits the exact `pveum` commands needed to close the gap and exits 0
    only after they have been applied (--apply).

Runs standalone on a Proxmox host (reads live state via `pvesh`/`pveum`),
or against fixture JSON for unit tests / CI (--realm-json / --acl-json /
--group-json). Never reads, logs, or prints a client secret: the realm
`client-key` field is dropped from any state this tool touches.

Deliberately NOT touched, ever (per the standard): a realm's
`username-claim`, `client-id`, `issuer-url`, `client-key`, or `default`
flag. Drift in those is never reported and never "corrected".

Exit codes (the contract a parity harness like live_parity relies on):
  0  aligned (or --apply succeeded)
  1  drift found (--check mode)
  2  usage / runtime error
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Iterable

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
# Keys this tool will never read for comparison or emit a change for.
NEVER_TOUCH_KEYS = {"username-claim", "client-id", "issuer-url", "client-key", "default"}


# ---------------------------------------------------------------------------
# Pure diff functions - no I/O, fully unit-testable.
# ---------------------------------------------------------------------------

def diff_realm(current: dict, target_keys: dict, target_scopes: set) -> dict:
    """Return the realm-level drift, ignoring NEVER_TOUCH_KEYS entirely."""
    missing_keys = {}
    for key, want in target_keys.items():
        if key in NEVER_TOUCH_KEYS:
            continue
        have = current.get(key)
        # Proxmox returns booleans as 0/1; normalize for comparison.
        if str(have) != str(want):
            missing_keys[key] = want

    have_scopes = set(str(current.get("scopes", "")).split())
    missing_scopes = set(target_scopes) - have_scopes

    aligned = not missing_keys and not missing_scopes
    return {"missing_keys": missing_keys, "missing_scopes": missing_scopes, "aligned": aligned}


def normalize_groups(raw) -> list:
    """Accept either raw `pvesh get /access/groups` output (list of dicts)
    or an already-reduced list of group-id strings."""
    out = []
    for item in raw:
        out.append(item["groupid"] if isinstance(item, dict) else item)
    return out


def diff_groups(current_groups: Iterable[str], target_groups: Iterable[str]) -> list:
    current_set = set(current_groups)
    return [g for g in target_groups if g not in current_set]


def diff_acls(current_acls: list, target_acls: list) -> list:
    have = {(a.get("path"), a.get("roleid"), a.get("ugid")) for a in current_acls}
    return [t for t in target_acls if t not in have]


def plan_commands(
    realm_name: str,
    realm_diff: dict,
    missing_groups: list,
    missing_acls: list,
    current_scopes: set | None = None,
) -> list:
    """Render the exact, minimal `pveum` commands needed to close the gap.

    Never references username-claim (or any NEVER_TOUCH_KEYS) - those are
    simply absent from `target_keys`/`target_scopes` by construction, and
    this function only ever emits what diff_* reported as missing.
    """
    commands = []

    realm_args = []
    for key, value in sorted(realm_diff.get("missing_keys", {}).items()):
        realm_args.append(f"--{key} {value}")

    if realm_diff.get("missing_scopes"):
        merged = (current_scopes or set()) | set(realm_diff["missing_scopes"])
        scopes_str = " ".join(sorted(merged))
        realm_args.append(f"--scopes '{scopes_str}'")

    if realm_args:
        commands.append(f"pveum realm modify {realm_name} " + " ".join(realm_args))

    for group in missing_groups:
        commands.append(
            f"pveum group add {group} "
            f"-comment 'SSO group (auto-managed by Authentik groups claim)'"
        )

    for path, role, group in missing_acls:
        commands.append(f"pveum acl modify {path} --roles {role} --groups {group}")

    return commands


def exit_code_for(aligned: bool) -> int:
    return 0 if aligned else 1


# ---------------------------------------------------------------------------
# Live-host I/O - isolated behind thin wrappers so tests never need a host.
# ---------------------------------------------------------------------------

def _run(cmd: list) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


def fetch_realm(realm_name: str) -> dict:
    data = json.loads(_run(["pvesh", "get", f"/access/domains/{realm_name}", "--output-format", "json"]))
    data.pop("client-key", None)  # never hold the secret in memory longer than needed
    return data


def fetch_acls() -> list:
    return json.loads(_run(["pvesh", "get", "/access/acl", "--output-format", "json"]))


def fetch_groups() -> list:
    data = json.loads(_run(["pvesh", "get", "/access/groups", "--output-format", "json"]))
    return normalize_groups(data)


def apply_commands(commands: list) -> None:
    for cmd in commands:
        # Commands are built entirely from the fixed target tables above -
        # never from free-form input - so splitting on whitespace is safe,
        # except for the quoted --scopes value.
        import shlex

        subprocess.run(shlex.split(cmd), check=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load(path_or_dash: str):
    if path_or_dash == "-":
        return json.load(sys.stdin)
    with open(path_or_dash) as fh:
        return json.load(fh)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--realm-name", default="authentik")
    parser.add_argument("--realm-json", help="fixture file (or '-' for stdin) instead of live `pvesh`")
    parser.add_argument("--acl-json", help="fixture file (or '-' for stdin) instead of live `pvesh`")
    parser.add_argument("--group-json", help="fixture file (or '-' for stdin) instead of live `pvesh`")
    parser.add_argument("--apply", action="store_true", help="apply the plan instead of just reporting it")
    parser.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    args = parser.parse_args(argv)

    try:
        realm = _load(args.realm_json) if args.realm_json else fetch_realm(args.realm_name)
        acls = _load(args.acl_json) if args.acl_json else fetch_acls()
        groups = normalize_groups(_load(args.group_json)) if args.group_json else fetch_groups()
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator, never a secret
        print(f"error: could not read current state: {exc}", file=sys.stderr)
        return 2

    realm_diff = diff_realm(realm, TARGET_REALM_KEYS, TARGET_SCOPES)
    missing_groups = diff_groups(groups, TARGET_GROUPS)
    missing_acls = diff_acls(acls, TARGET_ACLS)
    aligned = realm_diff["aligned"] and not missing_groups and not missing_acls

    current_scopes = set(str(realm.get("scopes", "")).split())
    plan = plan_commands(args.realm_name, realm_diff, missing_groups, missing_acls, current_scopes)

    report = {
        "realm": args.realm_name,
        "aligned": aligned,
        "realm_diff": {
            "missing_keys": realm_diff["missing_keys"],
            "missing_scopes": sorted(realm_diff["missing_scopes"]),
        },
        "missing_groups": missing_groups,
        "missing_acls": [list(t) for t in missing_acls],
        "plan": plan,
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        if aligned:
            print(f"OK: realm '{args.realm_name}' is aligned with the SKStacks proxmox-sso standard.")
        else:
            print(f"DRIFT: realm '{args.realm_name}' is not aligned.")
            for cmd in plan:
                print(f"  {cmd}")

    if args.apply and plan:
        apply_commands(plan)
        print(f"applied {len(plan)} command(s).")
        return 0

    return exit_code_for(aligned)


if __name__ == "__main__":
    sys.exit(main())
