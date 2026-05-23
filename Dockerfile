# Wave 6.4b — patchium Docker image.
#
# Multi-stage build to keep the runtime image lean. The builder stage installs
# Patchright + Chrome (~800 MB transient); the runtime stage copies just the
# wheel + system Chrome + venv.
#
# Build:    docker build -t patchium:0.3.0 .
# Run:      docker run -p 8000:8000 -p 9223:9223 patchium:0.3.0
#           # REST shim on 8000; live-view on 9223.
# Variants: --build-arg EXTRAS=all,nodriver for the optional stealth backend.

ARG PYTHON_VERSION=3.13
ARG EXTRAS=annotate,llm,liveview,secrets,rest

# ─── builder ────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder
ARG EXTRAS

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY patchium/ ./patchium/

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip build \
    && /opt/venv/bin/python -m build --wheel \
    && /opt/venv/bin/pip install "dist/$(ls dist | grep '\.whl$')[${EXTRAS}]"

# Install real Chrome via Patchright (saves us ~200 MB vs apt-get chrome stable)
RUN /opt/venv/bin/patchright install chrome --with-deps

# ─── runtime ────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

# Chrome runtime deps (without dev headers); Xvfb for headed mode.
# Match the list Patchright pulls during builder stage; keep slim.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
        fonts-liberation curl ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Copy the venv + Patchright-installed Chrome
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /root/.cache/ms-playwright /root/.cache/ms-playwright

ENV PATH=/opt/venv/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99
# Wave 6.1b: turn warm-pool ON by default in the image so first session start
# is snappy. Operator can override with -e PATCHIUM_WARM=off.
ENV PATCHIUM_WARM=both
# In a container, headless is the only sane default. Live-view + REST still work.
# Users who want headed mode can run with `-e DISPLAY=$DISPLAY -v /tmp/.X11-unix`.

WORKDIR /app

# Health check hits the REST shim
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/v1/health || exit 1

EXPOSE 8000 9223

# tini reaps zombies — Patchright + multi-session forks a lot of Chromes.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default: REST shim. Override with `docker run patchium:latest patchium mcp`
# for stdio MCP, or `... patchium start` for one-shot CLI use.
CMD ["sh", "-c", "Xvfb :99 -screen 0 1920x1080x24 & exec patchium serve --host 0.0.0.0 --port 8000"]
