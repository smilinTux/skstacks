"""
Guard tests for the OpenBao bootstrap — enforce the "no catch-22" rules.

The bootstrap must never leave the unseal keys or root token in plaintext on
disk: that would be a security hole AND the recovery secret would itself depend
on the secret store being up. We require PGP-encrypted init output (the operator
capauth PGP key is the only offline root needed to recover).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_BOOT = Path(__file__).resolve().parents[1] / "openbao" / "bootstrap.sh"


def test_bootstrap_script_exists():
    assert _BOOT.is_file(), f"missing {_BOOT}"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_bootstrap_script_is_valid_bash():
    # `bash -n` = syntax check without executing
    r = subprocess.run(["bash", "-n", str(_BOOT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_bootstrap_encrypts_init_output_no_catch22():
    text = _BOOT.read_text()
    # init must PGP-encrypt the unseal key shares AND the root token
    assert "-pgp-keys" in text, "unseal shares must be PGP-encrypted at init"
    assert "-root-token-pgp-key" in text, "root token must be PGP-encrypted at init"


def test_bootstrap_uses_serverless_root_of_trust():
    text = _BOOT.read_text().lower()
    # the recovery/root path must reference the offline roots (capauth/vault-file),
    # not a plaintext init json checked into a running-server dependency
    assert "capauth" in text or "vault-file" in text or "vault_file" in text


def test_bootstrap_unseals_every_raft_node_for_real_ha():
    # Redundancy: a 3-node Raft cluster where only the leader is unsealed has NO
    # failover (Shamir standbys stay sealed until unsealed too). The bootstrap must
    # unseal each replica, not just the default address.
    text = _BOOT.read_text()
    assert "REPLICAS" in text                       # iterates over the replica count
    # unseal must target each node's address, not only the default BAO_ADDR:
    # Swarm via tasks.<svc> task IPs, K8s via StatefulSet ordinals.
    assert "tasks.openbao" in text                  # Swarm: every task IP
    assert "openbao-" in text and "seq 0" in text   # K8s: every pod ordinal (0..N-1)
