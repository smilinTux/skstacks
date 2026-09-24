"""Entry point for `python -m skwire` and the single-file zipapp (skwire.pyz)."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
