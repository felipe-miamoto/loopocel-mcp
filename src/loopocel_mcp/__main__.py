"""Allow running loopocel_mcp as `python -m loopocel_mcp`."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

