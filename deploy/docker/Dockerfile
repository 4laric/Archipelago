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
CMD ["sh", "-c", "mkdir -p \"$DATA_DIR\" && exec gunicorn 'webgui.app:create_app()' -b 0.0.0.0:8080 --workers 1 --threads 8 --timeout 120"]
