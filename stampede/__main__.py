"""Support for running as ``python -m stampede``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
