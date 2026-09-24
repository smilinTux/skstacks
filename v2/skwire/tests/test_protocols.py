"""The formal extension points — the contracts an embedder implements."""
from __future__ import annotations

from skwire.protocols import SecretStore, Injector, Signer, Probe


def test_secretstore_protocol_is_satisfiable():
    class MyStore:
        def mint(self, key): return f"minted-{key}"
        def get(self, key): return "v"
        def put(self, key, value): pass
    assert isinstance(MyStore(), SecretStore)


def test_injector_protocol_is_satisfiable():
    class MyInjector:
        method = "env"
        def inject(self, target, key, value): return True
    assert isinstance(MyInjector(), Injector)


def test_signer_protocol_is_satisfiable():
    class MySigner:
        def sign(self, payload): return "sig"
    assert isinstance(MySigner(), Signer)


def test_probe_protocol_matches_the_preflight_probe():
    from skwire.preflight import FakeProbe
    assert isinstance(FakeProbe(), Probe)


def test_partial_impl_does_not_satisfy_protocol():
    class NotAStore:
        def mint(self, key): return "x"   # missing get/put
    assert not isinstance(NotAStore(), SecretStore)
