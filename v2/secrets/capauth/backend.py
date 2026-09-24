"""
SKStacks v2 — CapAuth / Sovereign PGP Backend
==============================================

Uses PGP-encrypted blobs as the secret store. Decryption is delegated to
the skcapstone MCP server (sovereign agent) running on the local machine.

Two operational modes:

  1. skcapstone MCP mode (default):
     Calls the skcapstone MCP tool `memory_search` + `memory_recall` to
     locate and decrypt secrets via HTTP POST (JSON-RPC 2.0) to the
     skcapstone MCP HTTP endpoint.  The agent's PGP private key never
     leaves the agent process.

  2. Direct GnuPG mode (fallback):
     Calls `gpg --decrypt` directly against encrypted blob files.
     Requires the correct private key in the local GnuPG keyring.

MCP → gnupg automatic fallback:
    If the skcapstone HTTP server is not reachable (ConnectionError) the
    backend transparently falls back to direct GnuPG operation so that
    deploy tooling keeps working even when the agent is offline.

Dependencies (mode 1):
    httpx>=0.27                 # pip install httpx
    skcapstone MCP HTTP server  # skcapstone mcp serve --http

Dependencies (mode 2):
    gnupg (system package)  # apt install gnupg / pacman -S gnupg
    pip install python-gnupg

Environment variables:
    CAPAUTH_MODE           "mcp" (default) or "gnupg"
    CAPAUTH_KEY_ID         PGP key fingerprint (gnupg mode + encryption fallback)
    CAPAUTH_AGENT          skcapstone agent name (mcp mode), default: opus
    CAPAUTH_SECRETS_DIR    Encrypted blob store, default: ~/.skstacks/secrets
    CAPAUTH_MCP_SOCKET     Unix socket for skcapstone MCP, default: ~/.skstacks/mcp.sock
    SKCAPSTONE_PORT        HTTP port for skcapstone MCP HTTP server, default: 9475

Blob layout (gnupg mode):
    {secrets_dir}/{env}/{scope}.gpg    (PGP-encrypted JSON: {"key": "value", ...})

Note:
    Secrets are stored as PGP-encrypted JSON blobs (gnupg mode) or as
    skcapstone memory entries tagged ["secret", scope, env] (mcp mode).
    Multiple recipients can be configured in capauth.yaml so any listed
    agent/operator can decrypt.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from ..interface import (
    SKSecretBackend,
    SecretMeta,
    SecretNotFoundError,
    SecretBackendError,
    SecretBackendAuthError,
)

logger = logging.getLogger("skstacks.capauth")


class CapAuthBackend(SKSecretBackend):
    """CapAuth sovereign PGP secret backend."""

    def __init__(
        self,
        mode: Optional[str] = None,
        key_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        secrets_dir: Optional[str] = None,
        mcp_socket: Optional[str] = None,
        mcp_port: Optional[int] = None,
    ):
        self._mode = (
            mode
            or os.environ.get("CAPAUTH_MODE", "mcp")
        ).lower()
        self._key_id = key_id or os.environ.get("CAPAUTH_KEY_ID")
        self._agent = agent_name or os.environ.get("CAPAUTH_AGENT", "opus")
        self._secrets_dir = Path(
            secrets_dir
            or os.environ.get("CAPAUTH_SECRETS_DIR", "~/.skstacks/secrets")
        ).expanduser()
        self._mcp_socket = mcp_socket or os.environ.get(
            "CAPAUTH_MCP_SOCKET", "~/.skstacks/mcp.sock"
        )
        port = mcp_port or int(os.environ.get("SKCAPSTONE_PORT", "9475"))
        self._mcp_url = f"http://127.0.0.1:{port}/mcp"

        if self._mode not in ("mcp", "gnupg"):
            raise ValueError(f"Unknown CapAuth mode: {self._mode!r}. Use 'mcp' or 'gnupg'.")

        if self._mode == "gnupg" and not self._key_id:
            raise SecretBackendAuthError(
                "CAPAUTH_KEY_ID must be set in gnupg mode."
            )

    # ── Blob path helpers ─────────────────────────────────────────────────────

    def _blob_path(self, scope: str, env: str) -> Path:
        return self._secrets_dir / env / f"{scope}.gpg"

    # ── gnupg decryption ──────────────────────────────────────────────────────

    def _decrypt_blob_gnupg(self, scope: str, env: str) -> dict[str, str]:
        blob = self._blob_path(scope, env)
        if not blob.exists():
            raise SecretBackendError(
                f"Encrypted blob not found: {blob}. "
                f"Run: capauth/scripts/init_scope.sh {env} {scope}"
            )
        result = subprocess.run(
            ["gpg", "--quiet", "--decrypt", str(blob)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise SecretBackendAuthError(
                f"GPG decryption failed for {blob}:\n{result.stderr}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SecretBackendError(f"Blob {blob} is not valid JSON: {exc}") from exc

    def _encrypt_blob_gnupg(
        self,
        scope: str,
        env: str,
        secrets: dict[str, str],
        recipients: Optional[list[str]] = None,
    ) -> None:
        """Encrypt secrets dict to a GPG blob. Recipients from capauth.yaml if not given."""
        if recipients is None:
            recipients = self._load_recipients(env)

        blob = self._blob_path(scope, env)
        blob.parent.mkdir(parents=True, exist_ok=True)

        recipient_args = []
        for r in recipients:
            recipient_args += ["-r", r]

        plaintext = json.dumps(secrets, indent=2).encode()
        result = subprocess.run(
            ["gpg", "--quiet", "--yes", "--encrypt", "--armor"] + recipient_args
            + ["--output", str(blob)],
            input=plaintext,
            capture_output=True,
        )
        if result.returncode != 0:
            raise SecretBackendError(
                f"GPG encryption failed: {result.stderr.decode()}"
            )

    def _load_recipients(self, env: str) -> list[str]:
        """Load PGP recipient fingerprints from capauth.yaml for an env."""
        config_path = self._secrets_dir.parent / "capauth.yaml"
        if not config_path.exists():
            if self._key_id:
                return [self._key_id]
            raise SecretBackendError(
                f"capauth.yaml not found at {config_path} and CAPAUTH_KEY_ID not set."
            )
        import yaml  # type: ignore[import-untyped]
        with open(config_path) as f:
            config = yaml.safe_load(f)
        env_cfg = config.get(env, config.get("default", {}))
        recipients = [r["fingerprint"] for r in env_cfg.get("recipients", [])]
        if not recipients:
            raise SecretBackendError(
                f"No recipients found for env={env!r} in {config_path}."
            )
        return recipients

    # ── MCP client (httpx → skcapstone HTTP) ──────────────────────────────────

    def _call_mcp(self, tool: str, args: dict):
        """
        Call a skcapstone MCP tool via HTTP POST (JSON-RPC 2.0).

        Sends a ``tools/call`` request to ``self._mcp_url`` and returns the
        parsed tool output extracted from the MCP ``TextContent`` response.

        Args:
            tool: MCP tool name, e.g. ``memory_search`` or ``memory_store``.
            args: Tool arguments matching the tool's ``inputSchema``.

        Returns:
            Parsed JSON value from the tool's TextContent response, or
            ``None`` if the response content list is empty.

        Raises:
            ConnectionError: skcapstone HTTP server is not reachable.
            SecretBackendError: JSON-RPC error or tool-level error response.
        """
        import httpx  # lazy — avoids hard dep at module load

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": args,
            },
        }
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(
                    self._mcp_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise ConnectionError(
                f"skcapstone MCP unreachable at {self._mcp_url}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise SecretBackendError(
                f"skcapstone MCP HTTP {exc.response.status_code}: {exc.response.text}"
            ) from exc

        data = resp.json()

        # JSON-RPC error object
        if "error" in data:
            err = data["error"]
            raise SecretBackendError(
                f"skcapstone MCP error [{err.get('code')}]: {err.get('message')}"
            )

        result = data.get("result", {})
        if result.get("isError"):
            content_text = (result.get("content") or [{}])[0].get("text", "")
            raise SecretBackendError(f"skcapstone tool error: {content_text}")

        content = result.get("content") or []
        if not content:
            return None

        text = content[0].get("text", "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    # ── MCP decryption / storage ───────────────────────────────────────────────

    def _decrypt_blob_mcp(self, scope: str, env: str) -> dict[str, str]:
        """
        Ask skcapstone MCP to find and return the secrets for a scope/env.

        Searches memories tagged [``secret``, *scope*, *env*], recalls the
        most recent match to retrieve the full JSON content dict.
        """
        results = self._call_mcp("memory_search", {
            "query": f"scope:{scope} env:{env}",
            "tags": ["secret", scope, env],
            "limit": 1,
        })
        if not results:
            raise SecretBackendError(
                f"No secret memory found in skcapstone for scope={scope!r} env={env!r}."
            )
        memory_id = results[0]["memory_id"]

        memory = self._call_mcp("memory_recall", {"memory_id": memory_id})
        try:
            return json.loads(memory["content"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise SecretBackendError(
                f"Invalid secret memory content for {scope}/{env}: {exc}"
            ) from exc

    def _store_blob_mcp(self, scope: str, env: str, secrets: dict[str, str]) -> None:
        """Store the secrets dict for a scope/env as a skcapstone memory entry."""
        self._call_mcp("memory_store", {
            "content": json.dumps(secrets),
            "importance": 0.9,
            "tags": ["secret", scope, env],
            "source": "skstacks-capauth",
        })

    # ── Internal dispatch ─────────────────────────────────────────────────────

    def _decrypt(self, scope: str, env: str) -> dict[str, str]:
        if self._mode == "mcp":
            try:
                return self._decrypt_blob_mcp(scope, env)
            except ConnectionError:
                logger.warning(
                    "skcapstone MCP unreachable at %s — falling back to gnupg",
                    self._mcp_url,
                )
                return self._decrypt_blob_gnupg(scope, env)
        return self._decrypt_blob_gnupg(scope, env)

    def _encrypt(self, scope: str, env: str, secrets: dict[str, str]) -> None:
        if self._mode == "mcp":
            try:
                self._store_blob_mcp(scope, env, secrets)
                return
            except ConnectionError:
                logger.warning(
                    "skcapstone MCP unreachable at %s — falling back to gnupg",
                    self._mcp_url,
                )
        self._encrypt_blob_gnupg(scope, env, secrets)

    # ── SKSecretBackend interface ─────────────────────────────────────────────

    def get(self, scope: str, env: str, key: str) -> str:
        secrets = self._decrypt(scope, env)
        if key not in secrets:
            raise SecretNotFoundError(scope, env, key)
        return secrets[key]

    def get_all(self, scope: str, env: str) -> dict[str, str]:
        return self._decrypt(scope, env)

    def get_with_meta(self, scope: str, env: str, key: str) -> tuple[str, SecretMeta]:
        value = self.get(scope, env, key)
        return value, SecretMeta(key=key, scope=scope, env=env)

    def set(self, scope: str, env: str, key: str, value: str) -> None:
        try:
            secrets = self._decrypt(scope, env)
        except SecretBackendError:
            secrets = {}
        secrets[key] = value
        self._encrypt(scope, env, secrets)

    def set_many(self, scope: str, env: str, secrets: dict[str, str]) -> None:
        try:
            existing = self._decrypt(scope, env)
        except SecretBackendError:
            existing = {}
        existing.update(secrets)
        self._encrypt(scope, env, existing)

    def delete(self, scope: str, env: str, key: str) -> None:
        secrets = self._decrypt(scope, env)
        if key not in secrets:
            raise SecretNotFoundError(scope, env, key)
        del secrets[key]
        self._encrypt(scope, env, secrets)

    def list_keys(self, scope: str, env: str) -> list[str]:
        return list(self._decrypt(scope, env).keys())

    def list_scopes(self, env: str) -> list[str]:
        env_dir = self._secrets_dir / env
        if not env_dir.exists():
            return []
        return [p.stem for p in env_dir.glob("*.gpg")]

    def health_check(self) -> dict:
        if self._mode == "gnupg":
            try:
                subprocess.run(["gpg", "--version"], capture_output=True, check=True)
                gpg_ok = True
            except (subprocess.CalledProcessError, FileNotFoundError):
                gpg_ok = False
            return {
                "status": "ok" if gpg_ok else "degraded",
                "backend": "capauth",
                "mode": "gnupg",
                "gnupg": "available" if gpg_ok else "not found",
                "secrets_dir": str(self._secrets_dir),
            }

        # MCP mode — probe the HTTP endpoint for liveness
        mcp_ok = False
        mcp_error: Optional[str] = None
        try:
            import httpx
            with httpx.Client(timeout=3.0) as client:
                # Any response (even error JSON) means the server is reachable.
                client.post(
                    self._mcp_url,
                    json={"jsonrpc": "2.0", "id": 0, "method": "ping", "params": {}},
                    headers={"Content-Type": "application/json"},
                )
            mcp_ok = True
        except Exception as exc:
            mcp_error = str(exc)

        try:
            subprocess.run(["gpg", "--version"], capture_output=True, check=True)
            gpg_ok = True
        except (subprocess.CalledProcessError, FileNotFoundError):
            gpg_ok = False

        status = "ok" if mcp_ok else ("degraded" if gpg_ok else "unavailable")
        result: dict = {
            "status": status,
            "backend": "capauth",
            "mode": "mcp",
            "agent": self._agent,
            "mcp_url": self._mcp_url,
            "mcp_reachable": mcp_ok,
            "gnupg_fallback": gpg_ok,
        }
        if mcp_error:
            result["mcp_error"] = mcp_error
        return result
