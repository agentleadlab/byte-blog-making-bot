# Playwright's own image, so Chromium and its system libraries are already here.
# The tag must track the playwright version pinned in pyproject.toml.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    WILBYTE_HOME=/app \
    WILBYTE_STATE_DIR=/data/state \
    WILBYTE_OUTPUT_DIR=/data/out \
    WILBYTE_CORPUS_DIR=/data/corpus

WORKDIR /app

# Dependencies first so a code change doesn't reinstall the world.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

COPY config/ ./config/
COPY prompts/ ./prompts/
COPY assets/ ./assets/

# /data holds the ledger, the rendered posts, and the copy library. Mount a
# volume here - without one the ledger resets on every redeploy (already-posted
# videos get posted again) and every piece of copy Byte has learned is lost.
RUN mkdir -p /data/state /data/out /data/corpus

CMD ["wilbyte", "bot"]
