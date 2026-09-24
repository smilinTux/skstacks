"""
hello_pack — a complete, copy-pasteable skwire Pack.

Shows the whole shape a project ships: descriptors (nodes), a project-specific
injector implementing the Injector contract, a tiny SecretStore, Socratic
questions, and the `pack` callable an entry point points at.

Try it:
    import skwire
    from hello_pack import pack
    skwire.register_pack(pack())
    plan = skwire.build_plan(skwire.all_nodes())
    print(skwire.explain(plan))
"""
from __future__ import annotations

import secrets as _secrets        # stdlib — for minting demo keys

from skwire import Pack


# --- a project-specific injector (implements the Injector contract) -----------
class EnvFileInjector:
    method = "file"

    def __init__(self):
        self.written: dict[str, str] = {}

    def inject(self, target: str, key: str, value: str) -> bool:
        # a real injector would write target's .env / call its API; here we record it
        self.written[f"{target}:{key}"] = value
        return True


# --- a tiny SecretStore (implements the SecretStore contract) -----------------
class DemoSecretStore:
    def __init__(self):
        self._d: dict[str, str] = {}

    def mint(self, key: str) -> str:
        self._d[key] = _secrets.token_hex(16)
        return self._d[key]

    def get(self, key: str) -> str:
        return self._d[key]

    def put(self, key: str, value: str) -> None:
        self._d[key] = value


def pack() -> Pack:
    """The entry point a project's pyproject points at."""
    return Pack(
        name="hello",
        nodes=[
            {"name": "greeter", "provides": {"url": "http://greeter:8080", "api_kind": "http"}},
            {"name": "app", "needs": [{"service": "greeter", "secret": "greeter_api_key"}]},
        ],
        injectors={"env-file": EnvFileInjector()},
        questions=["What should the greeter say? (default: 'hello, sovereign world')"],
    )
