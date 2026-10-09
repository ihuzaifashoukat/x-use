# Source-built MCP stdio image. The default target answers introspection:
#   docker build -t xuse .
# The optional browser-ready target installs Patchright's matching Chromium:
#   docker build --target browser -t xuse-browser .
# Both targets run as UID 10001. CI uses synthetic HTML and no X credentials.

FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install . && python -m pip check

RUN useradd --create-home --uid 10001 xuse \
    && mkdir -p /home/xuse/workspace \
    && chown -R xuse:xuse /home/xuse

USER xuse
WORKDIR /home/xuse/workspace
RUN x-use --help > /dev/null
ENTRYPOINT ["x-use", "mcp"]

FROM base AS browser
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN python -m patchright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright \
    && rm -rf /var/lib/apt/lists/*
USER xuse

# Preserve the small-image default; select --target browser for Chromium.
# Never bake cookies, PINs or API keys into either image. Use private runtime
# mounts for credentials and writable state directories owned by UID 10001.
# Use --init and --shm-size=1g with the browser target. MCP defaults to headless
# Patchright; the legacy Selenium CLI needs a separate browser/driver install.
FROM base AS runtime
