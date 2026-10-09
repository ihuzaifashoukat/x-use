#!/usr/bin/env python3
"""Render a valid stdio MCP client configuration for the installed command."""
from __future__ import annotations

import argparse
import json
import ntpath
import posixpath
from typing import Optional, Sequence


def render_config(command: str, data_home: str) -> str:
    """Return the portable JSON form used by Claude Desktop and Cursor."""
    if (not ntpath.isabs(command) and not posixpath.isabs(command)):
        raise ValueError("command path must be absolute")
    if (not ntpath.isabs(data_home) and not posixpath.isabs(data_home)):
        raise ValueError("data home must be absolute")
    return json.dumps({"mcpServers": {"x-use": {
        "command": command,
        "args": ["mcp"],
        "env": {"X_USE_HOME": data_home},
    }}}, indent=2, ensure_ascii=False)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", help="absolute x-use executable path")
    parser.add_argument("data_home", help="absolute x-use configuration and state directory")
    args = parser.parse_args(argv)
    print(render_config(args.command, args.data_home))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
