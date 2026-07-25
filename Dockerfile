# x-use as an MCP server over stdio.
#
# Built from source rather than from PyPI on purpose: directory scanners and CI
# should introspect the tools in *this* commit, not whatever version happens to
# be published. `pip install .` picks the version up from src/xuse/__init__.py.
#
# Scope: this image starts the server and answers introspection (initialize,
# tools/list, prompts/list, resources/list). That is all a registry check needs,
# and it is all the container promises. x-use drives a real logged-in Chrome, so
# tools that touch X need a browser plus an imported cookie file. See the
# "Browser automation" note at the bottom before deploying this anywhere.

FROM python:3.12-slim

# PYTHONUNBUFFERED matters here: the MCP transport is stdio, and a buffered
# stdout stalls JSON-RPC replies until the buffer flushes.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir .

# The server keeps config, drafts, the queue and logs under its working
# directory. Run as a non-root user with a writable home so a read-only or
# hardened runtime does not fail on first write.
RUN useradd --create-home --uid 10001 xuse \
    && mkdir -p /home/xuse/workspace \
    && chown -R xuse:xuse /home/xuse

USER xuse
WORKDIR /home/xuse/workspace

# Fails the build if the entry point is missing or the package cannot import.
RUN x-use --help > /dev/null

ENTRYPOINT ["x-use", "mcp"]

# Browser automation
# ------------------
# No browser is installed above. x-use exists to drive a real, logged-in Chrome
# session, and a bare Chromium in a fresh container is not logged in, so adding
# one would not by itself make the write tools work. To run the browser tools in
# a container you need both halves:
#
#   1. a browser and driver, e.g.
#        RUN apt-get update \
#         && apt-get install -y --no-install-recommends chromium chromium-driver \
#         && rm -rf /var/lib/apt/lists/*
#   2. a cookie file mounted in and imported by path, e.g.
#        docker run -v /host/cookies.json:/secrets/cookies.json:ro ...
#      then add_account(account_id="main", cookie_file="/secrets/cookies.json").
#
# Mount the cookie file, never bake it into an image layer and never paste its
# contents into a chat. Set "headless": true in the browser settings when there
# is no display.
