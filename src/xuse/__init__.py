"""x-use, browser-native AI agents for X (Twitter)."""

# Single source of truth for the version. pyproject.toml reads this through
# setuptools' dynamic-version attr, and tests/test_version_integrity.py checks
# server.json against it. Do not add a second literal anywhere: this one sat at
# 2.0.0 through three releases because nothing inside the package read it,
# while external scanners (MCP directories) did and reported 2.0.0.
__version__ = "2.3.1"
