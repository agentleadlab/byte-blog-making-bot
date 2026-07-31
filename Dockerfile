# Playwright's own image, so Chromium and its system libraries are already here.
# The tag must track the playwright version pinned in pyproject.toml.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    WILBYTE_HOME=/app \
    WILBYTE_STATE_DIR=/data/state \
    WILBYTE_OUTPUT_DIR=/data/out

WORKDIR /app

# Dependencies first so a code change doesn't reinstall the world.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

COPY config/ ./config/
COPY prompts/ ./prompts/
COPY assets/ ./assets/

# /data is where the ledger and rendered posts go. Mount a volume here, or the
# ledger resets on every redeploy and already-posted videos get posted again.
RUN mkdir -p /data/state /data/out

CMD ["wilbyte", "bot"]
