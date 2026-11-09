"""Allow running ocel4agent_mcp as `python -m ocel4agent_mcp`."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

