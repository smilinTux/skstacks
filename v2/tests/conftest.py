"""
pytest configuration for skstacks/v2/tests.

Ensures the local `secrets` package (skstacks/v2/secrets/) shadows the stdlib
`secrets` module so that `from secrets.factory import get_backend` resolves
to the local backend factory, not the standard library.
"""
import sys
from pathlib import Path

_V2_ROOT = Path(__file__).parent.parent

# Insert skstacks/v2/ at the front of sys.path before any test import
if str(_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(_V2_ROOT))

# Evict a cached stdlib `secrets` if it snuck in before our conftest ran
_cached = sys.modules.get("secrets")
if _cached is not None and not hasattr(_cached, "factory"):
    # Remove the stdlib version and all sub-entries so the local package loads
    stale = [k for k in sys.modules if k == "secrets" or k.startswith("secrets.")]
    for k in stale:
        del sys.modules[k]
