# Peliarch self-host image — web GUI + room hosting + Go Large-tier backend, one container.
# Build context is the REPO ROOT (see docker-compose.yml: context ../..).

# ---- stage 1: build the Go Large-tier backend ----
FROM golang:1.22-bookworm AS gobuild
WORKDIR /src/archipelago-go
COPY archipelago-go/go.mod archipelago-go/go.sum ./
RUN go mod download
COPY archipelago-go/ ./
RUN go build -o /out/peliarch .

# ---- stage 1b: the Elden Ring tooling, pinned ----
# The apworld and the wizard live in a DIFFERENT repo. Vendoring them here would fork them; copying
# them onto the box by hand is the manual step this whole file exists to delete. So they are cloned
# at a PIN during the build, and the pin is the reproducibility boundary: the same ER_REF always
# produces the same image, and moving it is a reviewable one-line diff rather than an ssh session.
#
# `tools/gf_test.py --install-only` is the ONE definition of "the installed world" -- the same entry
# point CI and the dev box use -- so the beside-package inputs (region_map.csv, the *.tsv tables,
# region_groups.py, the shipping yaml) cannot drift between how the host installs it and how it is
# tested. It deliberately performs no fork check in this mode, which is why it is safe to point at
# this repo's own tree.
FROM python:3.12-slim-bookworm AS ertools
ARG ER_REPO=https://github.com/4laric/er-archipelago.git
ARG ER_API=https://api.github.com/repos/4laric/er-archipelago
ARG ER_REF
COPY deploy/docker/validate-er-ref.sh /usr/local/bin/validate-er-ref
RUN validate-er-ref "$ER_REF"
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# 🛑 CACHE BUST, AND WHY IT IS NOT OPTIONAL.
# Docker caches a RUN by its command STRING. This cache-bust also makes the resolved immutable
# revision part of the build inputs, so changing ER_REF cannot reuse tooling from the old pin.
#
# `ADD <url>` is re-fetched on EVERY build and its content decides whether downstream layers are
# invalidated. ER_REF is restricted above to an immutable release tag or full commit SHA.
ADD ${ER_API}/commits/${ER_REF} /er-ref.json

RUN git init /er \
 && git -C /er remote add origin "$ER_REPO" \
 && git -C /er fetch --depth 1 origin "$ER_REF" \
 && git -C /er checkout --detach FETCH_HEAD \
 && git -C /er rev-parse HEAD > /er-rev.txt
# The world is installed into an empty tree here and copied into /app in stage 2, so a rebuild of
# stage 2 does not re-clone.
RUN mkdir -p /apworld && python /er/tools/gf_test.py --install-only --ap-dir /apworld
# 🛑 THE SERVED NAMES ARE THE CONTRACT, NOT THE REPO PATHS. app.py serves this directory verbatim
# and every link on the site is written against the served name -- /er/checks.html,
# /er/report.html, /er/tabs.js -- and `/` reads landing.html out of here. Copying the check browser
# under its repo name left /er/checks.html, the name the landing page links, a 404. Four of the
# five files below were simply absent, so a freshly built image answered 404 for the bug report
# form and the tab strip and 503 for its own front page.
#
# NO BLANKET `|| true`. The old line ended `2>/dev/null || true`, and that rescues the WHOLE `&&`
# chain: a missing wizard.html produced an image that built, started, and served a 404 as the front
# door. An empty result is a failure, so these five fail the build. The questline DAG gets its own
# line and keeps the tolerance, because it is the one artifact that may legitimately not exist at an
# older ER_REF.
#
# Keep a stable fallback in the image for direct/non-Compose runs. Production Compose mounts the
# host-managed deploy_wizard.sh output over /er-static, so page promotion does not rebuild rooms.
RUN mkdir -p /er-static \
 && cp /er/wizard/wizard.html  /er-static/wizard.html \
 && cp /er/wizard/landing.html /er-static/landing.html \
 && cp /er/wizard/report.html  /er-static/report.html \
 && cp /er/wizard/tabs.js      /er-static/tabs.js \
 && cp /er/er-archipelago-check-browser.html /er-static/checks.html
RUN cp /er/er-archipelago-questline-dag.html /er-static/questlines.html 2>/dev/null || true

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

# the Elden Ring apworld, into the tree /generate runs in, and the static tooling /er serves
COPY --from=ertools /apworld/worlds/eldenring /app/worlds/eldenring
COPY --from=ertools /er-static /er-static
COPY --from=ertools /er-rev.txt /app/.er-rev

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    STORE_PATH=/data/rooms.json \
    REPO_DIR=/app \
    AP_ROOT=/app \
    ER_STATIC_DIR=/er-static

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
