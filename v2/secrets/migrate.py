#!/usr/bin/env python3
"""
SKStacks v2 — Secret Backend Migration Tool
============================================

Migrate secrets between backends without exposing values to disk.

Usage:
    # vault-file → hashicorp-vault
    python3 secrets/migrate.py \\
        --from vault-file \\
        --to hashicorp-vault \\
        --env prod \\
        --scopes skfence sksec sksso skbackup skha

    # vault-file → capauth
    python3 secrets/migrate.py \\
        --from vault-file \\
        --to capauth \\
        --env prod

    # hashicorp-vault → capauth (full sovereignty migration)
    python3 secrets/migrate.py \\
        --from hashicorp-vault \\
        --to capauth \\
        --env prod \\
        --dry-run    ← preview without writing
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from secrets.factory import get_backend


def migrate(
    from_backend: str,
    to_backend: str,
    env: str,
    scopes: Optional[list[str]] = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict[str, int]:
    """
    Migrate all secrets for the given env from one backend to another.

    Args:
        from_backend: Source backend identifier
        to_backend:   Destination backend identifier
        env:          Environment (prod, staging, dev)
        scopes:       Specific scopes to migrate; None = all scopes
        dry_run:      If True, print what would be migrated without writing
        verbose:      Print individual key names

    Returns:
        Dict of {scope: key_count} for migrated scopes
    """
    print(f"[migrate] {from_backend} → {to_backend}  env={env}")
    if dry_run:
        print("[migrate] DRY RUN — no secrets will be written")

    src = get_backend(from_backend)
    dst = get_backend(to_backend)

    # Auto-discover scopes if not specified
    if not scopes:
        scopes = src.list_scopes(env)
        if not scopes:
            print(f"[migrate] No scopes found in source backend for env={env!r}.")
            return {}
        print(f"[migrate] Discovered scopes: {', '.join(scopes)}")

    results: dict[str, int] = {}

    for scope in scopes:
        print(f"\n[migrate] Scope: {scope}")
        try:
            secrets = src.get_all(scope, env)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR reading source: {exc}", file=sys.stderr)
            continue

        if not secrets:
            print(f"  (empty scope, skipping)")
            continue

        if verbose:
            for key in secrets:
                print(f"  key: {key}")
        else:
            print(f"  {len(secrets)} keys")

        if not dry_run:
            try:
                dst.set_many(scope, env, secrets)
                print(f"  ✓ written to {to_backend}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR writing to destination: {exc}", file=sys.stderr)
                continue

        results[scope] = len(secrets)

    total = sum(results.values())
    print(f"\n[migrate] {'Would have migrated' if dry_run else 'Migrated'} "
          f"{total} keys across {len(results)} scopes.")
    return results


def main() -> None:
    p = argparse.ArgumentParser(
        description="Migrate SKStacks secrets between backends.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[0].strip(),
    )
    p.add_argument("--from", dest="from_backend", required=True,
                   choices=["vault-file", "hashicorp-vault", "capauth"],
                   help="Source backend")
    p.add_argument("--to", dest="to_backend", required=True,
                   choices=["vault-file", "hashicorp-vault", "capauth"],
                   help="Destination backend")
    p.add_argument("--env", required=True,
                   choices=["prod", "staging", "dev"],
                   help="Environment to migrate")
    p.add_argument("--scopes", nargs="+",
                   help="Specific scopes to migrate (default: all)")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview migration without writing")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print individual key names (no values)")

    args = p.parse_args()

    if args.from_backend == args.to_backend:
        p.error("Source and destination backends are the same.")

    migrate(
        from_backend=args.from_backend,
        to_backend=args.to_backend,
        env=args.env,
        scopes=args.scopes,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
