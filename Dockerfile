# Portable Dockerfile for the expense-tracker bot.
#
# Targets (one image, three deploys):
#
#   * Hugging Face Spaces (the recommended free 24/7 host).
#     HF requires the container to listen on $PORT (default 7860) and
#     the Dockerfile to live at the repo root — both satisfied here.
#   * Render Free / Koyeb / Fly.io (drop-in fallback hosts).
#     Same image; they each inject $PORT differently and we honour it
#     via TELEGRAM_HEALTH_PORT.
#   * Local docker (for parity testing): `docker build -t expense .`
#     then `docker run --env-file .env expense`.
#
# Image philosophy:
#   * Slim Python base (~50 MB) — no compiler in final image.
#   * Two-stage: install deps in a builder, copy the venv to a clean
#     runtime image (saves ~200 MB on disk + every cold-start).
#   * Non-root user — defence in depth, even though HF runs us in a
#     sandbox already.

# ─── Stage 1: builder ──────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# Build deps for psycopg / google-auth (psycopg has a binary wheel so
# this is mostly belt-and-braces) — kept out of the final image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy ONLY metadata first → docker layer caching ignores src changes
# and re-uses the pip install layer when only application code changes.
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Single venv at /opt/venv → easy to copy across stages, lives outside
# the app dir so file-watchers on the source tree don't see it.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir ".[telegram]"

# ─── Stage 2: runtime ──────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Runtime deps only — libpq for psycopg and tini for clean PID-1
# signal handling so Ctrl-C / docker stop / HF restart actually kill
# the polling loop instead of leaving zombie children.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libpq5 \
        tini \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 bot

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Default health port — HF sets PORT=7860 itself which we pick up
    # below.  Keeping a default lets `docker run` work without flags.
    TELEGRAM_HEALTH_PORT=7860 \
    # Pin LOG_DIR to an absolute path the non-root user owns.  The
    # default ("./logs") would resolve relative to the CWD, which is
    # /app — and /app is root-owned, so the `bot` user can't mkdir
    # there.  Hosted platforms (HF Spaces, Render, Koyeb) all run our
    # user as non-root, so the relative default is a deploy-time
    # foot-gun.  Same dir doubles as the FX-rate cache home.
    LOG_DIR=/app/logs

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=bot:bot src/ ./src/
COPY --chown=bot:bot pyproject.toml README.md ./

# Pre-create the writable dirs the bot expects so the very first
# request doesn't race against mkdir + chown.  /app/logs holds JSONL
# traces and the FX cache; /app/data is reserved for future use
# (currency snapshots, local SQLite if a user opts in).
RUN mkdir -p /app/logs /app/data \
 && chown -R bot:bot /app/logs /app/data

USER bot

# Honour $PORT (Render / Koyeb / HF inject it) by exposing the same
# value as TELEGRAM_HEALTH_PORT at container start.  Tini reaps
# zombies and forwards SIGTERM cleanly to the polling loop.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/bin/sh", "-c", "TELEGRAM_HEALTH_PORT=${PORT:-${TELEGRAM_HEALTH_PORT:-7860}} expense --telegram"]

# Document the port so `docker run -P` works automatically.
EXPOSE 7860

# In-container healthcheck — independent of the platform's external
# probe, so `docker ps` shows accurate state during local testing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{__import__(\"os\").environ.get(\"TELEGRAM_HEALTH_PORT\",\"7860\")}/health',timeout=3).status==200 else 1)" \
    || exit 1
