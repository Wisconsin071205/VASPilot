"""``python -m vaspilot.mcp`` — stdio MCP server entry point."""

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
