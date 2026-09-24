"""
Integration tests for skstacks/v2/secrets/migrate.py
=====================================================

Coverage:
  1. Full vault-file → hashicorp-vault migration
  2. --dry-run shows diff without writing
  3. --env prod prefix filtering (auto-discovery + explicit scopes)
  4. Rollback behaviour on partial failure (best-effort, non-transactional)

Both source and destination backends are mocked via unittest.mock so that no
real Ansible Vault, HashiCorp Vault, or CapAuth infrastructure is required.

Run from skstacks/v2/:
    pytest tests/test_migrate.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

# conftest.py ensures skstacks/v2/ is on sys.path before these imports.
from secrets.interface import SecretBackendError
from secrets.migrate import migrate


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_backend(
    scopes: list[str] | None = None,
    secrets_by_scope: dict[str, dict[str, str]] | None = None,
) -> MagicMock:
    """
    Return a MagicMock satisfying the SKSecretBackend interface.

    - list_scopes() returns *scopes*.
    - get_all(scope, env) dispatches into *secrets_by_scope* when provided,
      otherwise returns {}.
    - set_many() is a no-op by default.
    """
    b = MagicMock()
    b.list_scopes.return_value = scopes or []
    if secrets_by_scope is not None:
        b.get_all.side_effect = lambda scope, env: secrets_by_scope[scope]
    else:
        b.get_all.return_value = {}
    b.set_many.return_value = None
    return b


# ── 1. Full vault-file → hashicorp-vault migration ───────────────────────────

class TestFullMigration:
    """Happy-path: all scopes read from vault-file and written to hashicorp-vault."""

    SCOPES_SECRETS: dict[str, dict[str, str]] = {
        "skfence":  {"cf_token": "tok_abc", "hetzner_key": "hz_def"},
        "sksec":    {"pgp_key": "pgp_xyz"},
        "skbackup": {"s3_key": "s3_111", "s3_secret": "s3_222", "bucket": "mybucket"},
    }

    def _make_src(self) -> MagicMock:
        return _make_backend(
            scopes=list(self.SCOPES_SECRETS),
            secrets_by_scope=self.SCOPES_SECRETS,
        )

    # ---- return value correctness ----

    @patch("secrets.migrate.get_backend")
    def test_returns_scope_key_counts(self, mock_gb: MagicMock) -> None:
        src, dst = self._make_src(), _make_backend()
        mock_gb.side_effect = [src, dst]

        result = migrate("vault-file", "hashicorp-vault", env="prod")

        assert result == {"skfence": 2, "sksec": 1, "skbackup": 3}

    @patch("secrets.migrate.get_backend")
    def test_total_key_count(self, mock_gb: MagicMock) -> None:
        src, dst = self._make_src(), _make_backend()
        mock_gb.side_effect = [src, dst]

        result = migrate("vault-file", "hashicorp-vault", env="prod")

        assert sum(result.values()) == 6

    # ---- destination write calls ----

    @patch("secrets.migrate.get_backend")
    def test_all_scopes_written_to_destination(self, mock_gb: MagicMock) -> None:
        src, dst = self._make_src(), _make_backend()
        mock_gb.side_effect = [src, dst]

        migrate("vault-file", "hashicorp-vault", env="prod")

        assert dst.set_many.call_count == 3
        for scope, secrets in self.SCOPES_SECRETS.items():
            dst.set_many.assert_any_call(scope, "prod", secrets)

    @patch("secrets.migrate.get_backend")
    def test_destination_not_read_during_migration(self, mock_gb: MagicMock) -> None:
        """migrate() must never call dst.get_all() — it only writes."""
        src, dst = self._make_src(), _make_backend()
        mock_gb.side_effect = [src, dst]

        migrate("vault-file", "hashicorp-vault", env="prod")

        dst.get_all.assert_not_called()

    # ---- backend instantiation order ----

    @patch("secrets.migrate.get_backend")
    def test_backends_instantiated_in_order(self, mock_gb: MagicMock) -> None:
        """get_backend("vault-file") must be called before get_backend("hashicorp-vault")."""
        src, dst = self._make_src(), _make_backend()
        mock_gb.side_effect = [src, dst]

        migrate("vault-file", "hashicorp-vault", env="prod")

        assert mock_gb.call_args_list == [
            call("vault-file"),
            call("hashicorp-vault"),
        ]

    # ---- source read calls ----

    @patch("secrets.migrate.get_backend")
    def test_src_get_all_called_once_per_scope(self, mock_gb: MagicMock) -> None:
        src, dst = self._make_src(), _make_backend()
        mock_gb.side_effect = [src, dst]

        migrate("vault-file", "hashicorp-vault", env="prod")

        assert src.get_all.call_count == len(self.SCOPES_SECRETS)
        for scope in self.SCOPES_SECRETS:
            src.get_all.assert_any_call(scope, "prod")

    @patch("secrets.migrate.get_backend")
    def test_no_scopes_returns_empty_dict(self, mock_gb: MagicMock) -> None:
        """Source with zero scopes → result is empty, no writes attempted."""
        src = _make_backend(scopes=[])
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        result = migrate("vault-file", "hashicorp-vault", env="prod")

        assert result == {}
        dst.set_many.assert_not_called()


# ── 2. --dry-run shows diff without writing ───────────────────────────────────

class TestDryRun:
    """dry_run=True must read the source but never call dst.set_many()."""

    SECRETS = {"db_pass": "s3cr3t", "api_key": "k3y_abc"}

    @patch("secrets.migrate.get_backend")
    def test_set_many_never_called(self, mock_gb: MagicMock) -> None:
        src = _make_backend(scopes=["skfence"], secrets_by_scope={"skfence": self.SECRETS})
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        migrate("vault-file", "hashicorp-vault", env="prod", dry_run=True)

        dst.set_many.assert_not_called()

    @patch("secrets.migrate.get_backend")
    def test_result_still_reports_would_be_counts(self, mock_gb: MagicMock) -> None:
        """Return value must reflect what *would have* been written."""
        src = _make_backend(scopes=["skfence"], secrets_by_scope={"skfence": self.SECRETS})
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        result = migrate("vault-file", "hashicorp-vault", env="prod", dry_run=True)

        assert result == {"skfence": len(self.SECRETS)}

    @patch("secrets.migrate.get_backend")
    def test_source_still_read_to_compute_diff(self, mock_gb: MagicMock) -> None:
        """dry_run must still call src.get_all() so it can report the diff."""
        src = _make_backend(scopes=["sksec"], secrets_by_scope={"sksec": {"k": "v"}})
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        migrate("vault-file", "hashicorp-vault", env="staging", dry_run=True)

        src.get_all.assert_called_once_with("sksec", "staging")

    @patch("secrets.migrate.get_backend")
    def test_dry_run_multiple_scopes_no_writes(self, mock_gb: MagicMock) -> None:
        secrets_map = {
            "skfence": {"a": "1"},
            "sksec":   {"b": "2", "c": "3"},
        }
        src = _make_backend(scopes=list(secrets_map), secrets_by_scope=secrets_map)
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        result = migrate("vault-file", "hashicorp-vault", env="prod", dry_run=True)

        dst.set_many.assert_not_called()
        assert result == {"skfence": 1, "sksec": 2}

    @patch("secrets.migrate.get_backend")
    def test_wet_run_does_write(self, mock_gb: MagicMock) -> None:
        """Sanity-check: when dry_run=False the same scenario does write."""
        src = _make_backend(scopes=["skfence"], secrets_by_scope={"skfence": self.SECRETS})
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        migrate("vault-file", "hashicorp-vault", env="prod", dry_run=False)

        dst.set_many.assert_called_once_with("skfence", "prod", self.SECRETS)


# ── 3. --env prod prefix filtering ────────────────────────────────────────────

class TestEnvFiltering:
    """env= is threaded through list_scopes, get_all, and set_many consistently."""

    # ---- auto-discovery via list_scopes ----

    @patch("secrets.migrate.get_backend")
    def test_list_scopes_called_with_correct_env(self, mock_gb: MagicMock) -> None:
        src = _make_backend(scopes=["skfence"], secrets_by_scope={"skfence": {"k": "v"}})
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        migrate("vault-file", "hashicorp-vault", env="prod")

        src.list_scopes.assert_called_once_with("prod")

    @patch("secrets.migrate.get_backend")
    def test_get_all_uses_prod_env_for_every_scope(self, mock_gb: MagicMock) -> None:
        secrets_map = {
            "skfence": {"k": "v"},
            "sksec":   {"x": "y"},
        }
        src = _make_backend(scopes=list(secrets_map), secrets_by_scope=secrets_map)
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        migrate("vault-file", "hashicorp-vault", env="prod")

        for args, _ in src.get_all.call_args_list:
            assert args[1] == "prod", f"Expected env='prod', got {args[1]!r}"

    @patch("secrets.migrate.get_backend")
    def test_set_many_uses_prod_env_for_every_scope(self, mock_gb: MagicMock) -> None:
        src = _make_backend(
            scopes=["skfence", "sksec"],
            secrets_by_scope={"skfence": {"k": "v"}, "sksec": {"x": "y"}},
        )
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        migrate("vault-file", "hashicorp-vault", env="prod")

        for args, _ in dst.set_many.call_args_list:
            assert args[1] == "prod", f"Expected env='prod', got {args[1]!r}"

    @patch("secrets.migrate.get_backend")
    def test_staging_env_threaded_through(self, mock_gb: MagicMock) -> None:
        """Non-prod env must be propagated identically."""
        src = _make_backend(scopes=["skdev"], secrets_by_scope={"skdev": {"k": "v"}})
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        migrate("vault-file", "hashicorp-vault", env="staging")

        src.list_scopes.assert_called_once_with("staging")
        src.get_all.assert_called_once_with("skdev", "staging")
        dst.set_many.assert_called_once_with("skdev", "staging", {"k": "v"})

    # ---- explicit scopes bypass list_scopes ----

    @patch("secrets.migrate.get_backend")
    def test_explicit_scopes_bypass_list_scopes(self, mock_gb: MagicMock) -> None:
        src = _make_backend(secrets_by_scope={"skfence": {"k": "v"}})
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        migrate("vault-file", "hashicorp-vault", env="prod", scopes=["skfence"])

        src.list_scopes.assert_not_called()

    @patch("secrets.migrate.get_backend")
    def test_explicit_scopes_only_migrates_named_scopes(self, mock_gb: MagicMock) -> None:
        """Scopes not in the explicit list are not touched even if they exist in src."""
        secrets_map = {
            "skfence":  {"a": "1"},
            "sksec":    {"b": "2"},
            "skbackup": {"c": "3"},   # should NOT be migrated
        }
        src = _make_backend(scopes=list(secrets_map), secrets_by_scope=secrets_map)
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        result = migrate(
            "vault-file", "hashicorp-vault", env="prod",
            scopes=["skfence", "sksec"],
        )

        assert set(result.keys()) == {"skfence", "sksec"}
        written_scopes = [args[0] for args, _ in dst.set_many.call_args_list]
        assert "skbackup" not in written_scopes

    @patch("secrets.migrate.get_backend")
    def test_explicit_single_scope_result(self, mock_gb: MagicMock) -> None:
        src = _make_backend(
            secrets_by_scope={"skfence": {"a": "1", "b": "2", "c": "3"}},
        )
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        result = migrate(
            "vault-file", "hashicorp-vault", env="prod",
            scopes=["skfence"],
        )

        assert result == {"skfence": 3}
        dst.set_many.assert_called_once_with(
            "skfence", "prod", {"a": "1", "b": "2", "c": "3"}
        )


# ── 4. Rollback on partial failure ────────────────────────────────────────────

class TestPartialFailure:
    """
    migrate() is intentionally best-effort, not transactional.

    When a scope fails (read or write), migration continues to the next scope.
    Already-written scopes are NOT rolled back — there is no compensating
    set_many() or delete() call after an error.
    """

    # ---- write-side failure ----

    @patch("secrets.migrate.get_backend")
    def test_write_failure_on_one_scope_continues_to_next(self, mock_gb: MagicMock) -> None:
        """set_many() failure for scope A must not prevent scope B from being written."""
        secrets_map = {
            "skfence": {"a": "1"},
            "sksec":   {"b": "2"},
        }
        src = _make_backend(scopes=["skfence", "sksec"], secrets_by_scope=secrets_map)
        dst = _make_backend()

        def _fail_on_skfence(scope: str, env: str, secrets: dict) -> None:
            if scope == "skfence":
                raise SecretBackendError("vault sealed")

        dst.set_many.side_effect = _fail_on_skfence
        mock_gb.side_effect = [src, dst]

        result = migrate("vault-file", "hashicorp-vault", env="prod")

        dst.set_many.assert_any_call("sksec", "prod", {"b": "2"})
        assert "skfence" not in result
        assert result.get("sksec") == 1

    @patch("secrets.migrate.get_backend")
    def test_failed_scope_excluded_from_result(self, mock_gb: MagicMock) -> None:
        src = _make_backend(
            scopes=["good", "bad"],
            secrets_by_scope={"good": {"k": "v"}, "bad": {"k": "v"}},
        )
        dst = _make_backend()

        def _fail_on_bad(scope: str, env: str, secrets: dict) -> None:
            if scope == "bad":
                raise SecretBackendError("write error")

        dst.set_many.side_effect = _fail_on_bad
        mock_gb.side_effect = [src, dst]

        result = migrate("vault-file", "hashicorp-vault", env="prod")

        assert "bad" not in result
        assert "good" in result

    # ---- read-side failure ----

    @patch("secrets.migrate.get_backend")
    def test_read_failure_skips_scope_continues_to_next(self, mock_gb: MagicMock) -> None:
        """src.get_all() failure for scope A must not prevent scope B from being read/written."""
        src = _make_backend(scopes=["bad_scope", "good_scope"])

        def _fail_on_bad(scope: str, env: str) -> dict[str, str]:
            if scope == "bad_scope":
                raise SecretBackendError("vault file corrupted")
            return {"healthy_key": "val"}

        src.get_all.side_effect = _fail_on_bad
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        result = migrate("vault-file", "hashicorp-vault", env="prod")

        dst.set_many.assert_called_once_with("good_scope", "prod", {"healthy_key": "val"})
        assert "bad_scope" not in result
        assert result.get("good_scope") == 1

    # ---- no rollback of previously-written scopes ----

    @patch("secrets.migrate.get_backend")
    def test_no_rollback_of_already_written_scopes(self, mock_gb: MagicMock) -> None:
        """
        scope_ok_1 writes successfully, scope_fail raises, scope_ok_2 then writes.
        At no point should set_many() be called to undo scope_ok_1.
        """
        secrets_map = {
            "scope_ok_1": {"k1": "v1"},
            "scope_fail": {"k2": "v2"},
            "scope_ok_2": {"k3": "v3"},
        }
        src = _make_backend(
            scopes=["scope_ok_1", "scope_fail", "scope_ok_2"],
            secrets_by_scope=secrets_map,
        )
        dst = _make_backend()
        write_log: list[str] = []

        def _track_writes(scope: str, env: str, secrets: dict) -> None:
            if scope == "scope_fail":
                raise SecretBackendError("connection reset")
            write_log.append(scope)

        dst.set_many.side_effect = _track_writes
        mock_gb.side_effect = [src, dst]

        result = migrate("vault-file", "hashicorp-vault", env="prod")

        # Both ok scopes were written
        assert "scope_ok_1" in write_log
        assert "scope_ok_2" in write_log
        # Failed scope did not write
        assert "scope_fail" not in write_log
        # No delete() / compensating call issued
        dst.delete.assert_not_called()
        # Result reflects only the successful scopes
        assert result == {"scope_ok_1": 1, "scope_ok_2": 1}

    @patch("secrets.migrate.get_backend")
    def test_all_scopes_fail_returns_empty_result(self, mock_gb: MagicMock) -> None:
        src = _make_backend(
            scopes=["s1", "s2"],
            secrets_by_scope={"s1": {"k": "v"}, "s2": {"k": "v"}},
        )
        dst = _make_backend()
        dst.set_many.side_effect = SecretBackendError("vault unavailable")
        mock_gb.side_effect = [src, dst]

        result = migrate("vault-file", "hashicorp-vault", env="prod")

        assert result == {}

    @patch("secrets.migrate.get_backend")
    def test_empty_scope_silently_skipped_not_written(self, mock_gb: MagicMock) -> None:
        """A scope whose get_all() returns {} is skipped; dst.set_many is not called for it."""
        src = _make_backend(
            scopes=["empty_scope", "real_scope"],
            secrets_by_scope={"empty_scope": {}, "real_scope": {"k": "v"}},
        )
        dst = _make_backend()
        mock_gb.side_effect = [src, dst]

        result = migrate("vault-file", "hashicorp-vault", env="prod")

        assert "empty_scope" not in result
        dst.set_many.assert_called_once_with("real_scope", "prod", {"k": "v"})

    @patch("secrets.migrate.get_backend")
    def test_partial_failure_total_reported_correctly(self, mock_gb: MagicMock) -> None:
        """Sum of result values reflects only successfully-migrated keys."""
        secrets_map = {
            "scope_a": {"a1": "v", "a2": "v"},   # 2 keys, will succeed
            "scope_b": {"b1": "v"},               # will fail
            "scope_c": {"c1": "v", "c2": "v", "c3": "v"},  # 3 keys, will succeed
        }
        src = _make_backend(scopes=list(secrets_map), secrets_by_scope=secrets_map)
        dst = _make_backend()

        def _fail_b(scope: str, env: str, secrets: dict) -> None:
            if scope == "scope_b":
                raise SecretBackendError("network error")

        dst.set_many.side_effect = _fail_b
        mock_gb.side_effect = [src, dst]

        result = migrate("vault-file", "hashicorp-vault", env="prod")

        assert sum(result.values()) == 5   # 2 + 3; scope_b excluded
        assert result == {"scope_a": 2, "scope_c": 3}
