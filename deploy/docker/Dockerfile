# Peliarch self-host image — web GUI + room hosting + Go Large-tier backend, one container.
# Build context is the REPO ROOT (see docker-compose.yml: context ../..).

# ---- stage 1: build the Go Large-tier backend ----
FROM golang:1.22-bookworm AS gobuild
WORKDIR /src/archipelago-go
COPY archipelago-go/go.mod archipelago-go/go.sum ./
RUN go mod download
COPY archipelago-go/ ./
RUN go build -o /out/peliarch .

# ---- stage 2: python runtime (web GUI + per-room MultiServer) ----
FROM python:3.12-slim-bookworm
WORKDIR /app

# gcc covers source-only wheels (jellyfish etc.); slim keeps the image small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc \
 && rm -rf /var/lib/apt/lists/*

COPY deploy/docker/requirements-host.txt ./
RUN pip install --no-cache-dir -r requirements-host.txt

# the whole AP checkout (MultiServer.py, Utils.py, worlds/, webgui/, ...)
COPY . /app
# drop in the freshly-built Go binary
COPY --from=gobuild /out/peliarch /app/archipelago-go/peliarch

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    STORE_PATH=/data/rooms.json \
    REPO_DIR=/app

EXPOSE 8080

# ONE worker: the orchestrator keeps room state + the port allocator in-memory and
# supervises subprocesses, so it must be a single process. Threads handle the SSE log
# tails and concurrent admin requests.
#
# THREAD BUDGET = MAX CONCURRENT SSE STREAMS. Each open /room/<id>/logs?stream=1
# holds a thread for the life of the connection, so the thread count is a hard cap on
# simultaneous log viewers. Once every thread is parked on a stream the whole site
# stops responding — and it fails SILENTLY: gunicorn's --timeout only fires on workers
# that stop heartbeating, and a gthread worker keeps heartbeating from its main loop
# while all request threads are blocked. No crash, no log line, container still "Up".
# This wedged the site for ~3 weeks (2026-07-17 → 2026-08-07) at the previous value of 8.
# Raising the ceiling buys headroom; the real fix is capping SSE stream lifetime in
# webgui/app.py so abandoned browser tabs release their thread.
CMD ["sh", "-c", "mkdir -p \"$DATA_DIR\" && exec gunicorn 'webgui.app:create_app()' -b 0.0.0.0:8080 --workers 1 --threads 64 --timeout 120"]
