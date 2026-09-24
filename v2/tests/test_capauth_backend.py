"""
Unit tests for skstacks/v2/secrets/capauth/backend.py

Covers:
  - _call_mcp: happy path, JSON-RPC error, HTTP error, ConnectionError
  - get() / set() via MCP (mocked _call_mcp)
  - get() / set() fallback to gnupg on ConnectionError
  - set() / set_many() read-merge-write semantics
  - health_check() in both modes
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure skstacks/v2/ is on sys.path (conftest.py already handles this for
# pytest invocations, but keep explicit for IDE direct-run)
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from secrets.capauth.backend import CapAuthBackend
from secrets.interface import SecretBackendError, SecretNotFoundError


# ── helpers ───────────────────────────────────────────────────────────────────

def _mcp_backend(tmp_path: Path, **kwargs) -> CapAuthBackend:
    return CapAuthBackend(
        mode="mcp",
        secrets_dir=str(tmp_path / "secrets"),
        mcp_port=9475,
        **kwargs,
    )


def _memory_search_response(scope: str, env: str, content: dict) -> list:
    """Simulate _call_mcp("memory_search", ...) returning one result."""
    return [{"memory_id": "mem-abc123", "tags": ["secret", scope, env]}]


def _memory_recall_response(content: dict) -> dict:
    """Simulate _call_mcp("memory_recall", ...) returning a memory entry."""
    return {"memory_id": "mem-abc123", "content": json.dumps(content)}


# ── _call_mcp unit tests ──────────────────────────────────────────────────────

class TestCallMcp:
    def test_happy_path_returns_parsed_json(self, tmp_path):
        backend = _mcp_backend(tmp_path)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": '["foo", "bar"]'}],
                "isError": False,
            },
        }
        mock_resp.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
            result = backend._call_mcp("memory_search", {"query": "test"})

        assert result == ["foo", "bar"]

    def test_raises_connection_error_on_connect_failure(self, tmp_path):
        import httpx

        backend = _mcp_backend(tmp_path)

        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.side_effect = (
                httpx.ConnectError("refused")
            )
            with pytest.raises(ConnectionError, match="skcapstone MCP unreachable"):
                backend._call_mcp("memory_search", {"query": "test"})

    def test_raises_backend_error_on_jsonrpc_error(self, tmp_path):
        backend = _mcp_backend(tmp_path)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        }
        mock_resp.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
            with pytest.raises(SecretBackendError, match="Method not found"):
                backend._call_mcp("memory_search", {"query": "test"})

    def test_raises_backend_error_on_tool_error(self, tmp_path):
        backend = _mcp_backend(tmp_path)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "query is required"}],
                "isError": True,
            },
        }
        mock_resp.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
            with pytest.raises(SecretBackendError, match="query is required"):
                backend._call_mcp("memory_search", {})

    def test_returns_none_for_empty_content(self, tmp_path):
        backend = _mcp_backend(tmp_path)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [], "isError": False},
        }
        mock_resp.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
            result = backend._call_mcp("memory_store", {"content": "x"})

        assert result is None


# ── get() via MCP ─────────────────────────────────────────────────────────────

class TestGetMcp:
    def test_get_returns_value_from_mcp(self, tmp_path):
        backend = _mcp_backend(tmp_path)
        secrets = {"db_pass": "hunter2", "api_key": "sk-abc"}

        def fake_call_mcp(tool, args):
            if tool == "memory_search":
                return _memory_search_response("myapp", "prod", secrets)
            if tool == "memory_recall":
                return _memory_recall_response(secrets)
            raise AssertionError(f"Unexpected tool: {tool}")

        backend._call_mcp = fake_call_mcp
        assert backend.get("myapp", "prod", "db_pass") == "hunter2"

    def test_get_raises_not_found_for_missing_key(self, tmp_path):
        backend = _mcp_backend(tmp_path)
        secrets = {"other_key": "value"}

        def fake_call_mcp(tool, args):
            if tool == "memory_search":
                return _memory_search_response("myapp", "prod", secrets)
            if tool == "memory_recall":
                return _memory_recall_response(secrets)

        backend._call_mcp = fake_call_mcp
        with pytest.raises(SecretNotFoundError):
            backend.get("myapp", "prod", "missing")

    def test_get_raises_backend_error_when_no_memory(self, tmp_path):
        backend = _mcp_backend(tmp_path)

        def fake_call_mcp(tool, args):
            if tool == "memory_search":
                return []  # empty — no memory found

        backend._call_mcp = fake_call_mcp
        with pytest.raises(SecretBackendError, match="No secret memory found"):
            backend.get("myapp", "prod", "db_pass")


# ── set() via MCP — read-merge-write semantics ───────────────────────────────

class TestSetMcp:
    def test_set_merges_with_existing_secrets(self, tmp_path):
        backend = _mcp_backend(tmp_path)
        existing = {"db_pass": "old", "api_key": "sk-abc"}
        stored: list[dict] = []

        def fake_call_mcp(tool, args):
            if tool == "memory_search":
                return _memory_search_response("myapp", "prod", existing)
            if tool == "memory_recall":
                return _memory_recall_response(existing)
            if tool == "memory_store":
                stored.append(json.loads(args["content"]))
                return None
            raise AssertionError(f"Unexpected tool: {tool}")

        backend._call_mcp = fake_call_mcp
        backend.set("myapp", "prod", "db_pass", "new_pass")

        assert len(stored) == 1
        assert stored[0]["db_pass"] == "new_pass"
        assert stored[0]["api_key"] == "sk-abc"  # existing key preserved

    def test_set_creates_new_scope_when_none_exists(self, tmp_path):
        backend = _mcp_backend(tmp_path)
        stored: list[dict] = []

        def fake_call_mcp(tool, args):
            if tool == "memory_search":
                return []  # no existing memory
            if tool == "memory_store":
                stored.append(json.loads(args["content"]))
                return None
            raise AssertionError(f"Unexpected tool: {tool}")

        backend._call_mcp = fake_call_mcp
        backend.set("newapp", "dev", "token", "abc123")

        assert stored == [{"token": "abc123"}]

    def test_set_many_merges_and_writes(self, tmp_path):
        backend = _mcp_backend(tmp_path)
        existing = {"key1": "v1", "key2": "v2"}
        stored: list[dict] = []

        def fake_call_mcp(tool, args):
            if tool == "memory_search":
                return _memory_search_response("app", "prod", existing)
            if tool == "memory_recall":
                return _memory_recall_response(existing)
            if tool == "memory_store":
                stored.append(json.loads(args["content"]))
                return None

        backend._call_mcp = fake_call_mcp
        backend.set_many("app", "prod", {"key2": "new2", "key3": "v3"})

        assert stored[0] == {"key1": "v1", "key2": "new2", "key3": "v3"}


# ── gnupg fallback on ConnectionError ────────────────────────────────────────

class TestFallback:
    def _write_blob(self, backend: CapAuthBackend, scope: str, env: str, data: dict):
        """Write a plaintext JSON 'blob' bypassing gpg (for testing fallback path)."""
        blob = backend._blob_path(scope, env)
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_text(json.dumps(data))

    def test_get_falls_back_to_gnupg_on_connection_error(self, tmp_path):
        backend = _mcp_backend(tmp_path)

        def fake_call_mcp(tool, args):
            raise ConnectionError("skcapstone MCP unreachable")

        backend._call_mcp = fake_call_mcp

        # Override gnupg decryption to return plaintext (avoids requiring gpg binary)
        fake_secrets = {"secret": "fallback_value"}
        backend._decrypt_blob_gnupg = lambda s, e: fake_secrets

        result = backend.get("myapp", "prod", "secret")
        assert result == "fallback_value"

    def test_set_falls_back_to_gnupg_encrypt_on_connection_error(self, tmp_path):
        backend = _mcp_backend(tmp_path)
        gnupg_calls: list[tuple] = []

        def fake_call_mcp(tool, args):
            if tool == "memory_search":
                raise ConnectionError("unreachable")  # triggers fallback in _decrypt
            raise ConnectionError("unreachable")

        def fake_decrypt_gnupg(scope, env):
            return {}  # no existing blob

        def fake_encrypt_gnupg(scope, env, secrets, recipients=None):
            gnupg_calls.append((scope, env, secrets))

        backend._call_mcp = fake_call_mcp
        backend._decrypt_blob_gnupg = fake_decrypt_gnupg
        backend._encrypt_blob_gnupg = fake_encrypt_gnupg

        backend.set("myapp", "prod", "key", "value")

        assert gnupg_calls == [("myapp", "prod", {"key": "value"})]


# ── health_check ──────────────────────────────────────────────────────────────

class TestHealthCheck:
    def test_gnupg_mode_ok(self, tmp_path):
        backend = CapAuthBackend(
            mode="gnupg",
            key_id="DEADBEEF",
            secrets_dir=str(tmp_path / "secrets"),
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = backend.health_check()

        assert result["status"] == "ok"
        assert result["mode"] == "gnupg"
        assert result["gnupg"] == "available"

    def test_mcp_mode_reachable(self, tmp_path):
        backend = _mcp_backend(tmp_path)
        with patch("httpx.Client") as mock_client_cls, \
             patch("subprocess.run") as mock_run:
            mock_client_cls.return_value.__enter__.return_value.post.return_value = MagicMock()
            mock_run.return_value = MagicMock(returncode=0)
            result = backend.health_check()

        assert result["status"] == "ok"
        assert result["mcp_reachable"] is True
        assert result["gnupg_fallback"] is True

    def test_mcp_mode_unreachable_but_gnupg_available(self, tmp_path):
        import httpx

        backend = _mcp_backend(tmp_path)
        with patch("httpx.Client") as mock_client_cls, \
             patch("subprocess.run") as mock_run:
            mock_client_cls.return_value.__enter__.return_value.post.side_effect = (
                httpx.ConnectError("refused")
            )
            mock_run.return_value = MagicMock(returncode=0)
            result = backend.health_check()

        assert result["status"] == "degraded"
        assert result["mcp_reachable"] is False
        assert result["gnupg_fallback"] is True
        assert "mcp_error" in result

    def test_mcp_mode_fully_unavailable(self, tmp_path):
        import httpx

        backend = _mcp_backend(tmp_path)
        with patch("httpx.Client") as mock_client_cls, \
             patch("subprocess.run") as mock_run:
            mock_client_cls.return_value.__enter__.return_value.post.side_effect = (
                httpx.ConnectError("refused")
            )
            mock_run.side_effect = FileNotFoundError("gpg not found")
            result = backend.health_check()

        assert result["status"] == "unavailable"
        assert result["mcp_reachable"] is False
        assert result["gnupg_fallback"] is False
