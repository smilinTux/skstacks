#!/usr/bin/env python3
"""
skwire all-in-one installer — cross-platform (Windows / macOS / Linux).

    python install.py            # pip-install skwire + verify
    python install.py --from .   # install from a local checkout

skwire's core is pure-stdlib (zero runtime deps), so this just pip-installs the
package and runs a smoke `skwire scan`. No compilers, no system packages.
"""
import subprocess
import sys


def main() -> int:
    if sys.version_info < (3, 10):
        print("Python 3.10+ required"); return 1
    source = "."
    if "--from" in sys.argv:
        source = sys.argv[sys.argv.index("--from") + 1]
    elif "skwire" not in (sys.argv + [""]):
        source = "skwire"  # PyPI (once published); use --from . for a checkout
    print(f"==> installing skwire from {source!r} …")
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "--user", source])
    if rc != 0:
        return rc
    print("==> verifying …")
    subprocess.call([sys.executable, "-m", "skwire.cli", "scan"])
    print("✓ skwire installed. Try:  skwire scan | skwire packs | skwire plan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
