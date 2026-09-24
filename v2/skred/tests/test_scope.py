"""The scope guard is skred's safety rail: red-team tooling may ONLY ever touch our
own targets. Anything not explicitly in scope is refused — fail closed."""
from __future__ import annotations

import pytest

from skred.scope import ScopeGuard, OutOfScopeError


@pytest.fixture
def guard():
    return ScopeGuard(
        domains=["skworld.io", "example.org", "skstack01.example.org"],
        cidrs=["192.168.100.0/24", "127.0.0.0/8", "10.0.0.0/8"],
        roots=["/tmp/skstacks-build"],
    )


def test_own_domain_and_subdomains_are_in_scope(guard):
    assert guard.is_allowed("skwire.skworld.io")
    assert guard.is_allowed("https://skgit.skstack01.example.org/x")
    assert guard.is_allowed("example.org")


def test_lan_and_loopback_ips_are_in_scope(guard):
    assert guard.is_allowed("192.168.100.158")
    assert guard.is_allowed("127.0.0.1")
    assert guard.is_allowed("http://10.1.2.3:8200/")


def test_external_domains_are_refused(guard):
    assert not guard.is_allowed("google.com")
    assert not guard.is_allowed("https://evil.example.com/pwn")
    # a look-alike suffix must NOT pass (not-skworld.io is a different domain)
    assert not guard.is_allowed("notskworld.io")
    assert not guard.is_allowed("skworld.io.attacker.com")


def test_external_ips_are_refused(guard):
    assert not guard.is_allowed("8.8.8.8")
    assert not guard.is_allowed("https://1.2.3.4/")


def test_paths_must_be_under_an_allowed_root(guard):
    assert guard.is_allowed("/tmp/skstacks-build/v2/skred/scope.py")
    assert not guard.is_allowed("/etc/passwd")
    assert not guard.is_allowed("/home/alice/.ssh/id_rsa")


def test_assert_in_scope_raises_on_external(guard):
    guard.assert_in_scope("192.168.100.59")            # ok, no raise
    with pytest.raises(OutOfScopeError):
        guard.assert_in_scope("api.openai.com")


def test_empty_scope_fails_closed():
    # a guard with no allowlist must refuse EVERYTHING (never default-open)
    g = ScopeGuard(domains=[], cidrs=[], roots=[])
    assert not g.is_allowed("192.168.100.1")
    assert not g.is_allowed("skworld.io")
    assert not g.is_allowed("/tmp/x")
