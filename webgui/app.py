"""
app.py — Flask web app for Peliarch self-host GUI.

Implements:
  HTML pages:  GET /              the Elden Ring landing page (static, from ER_STATIC_DIR)
               GET /hosting      the rooms dashboard: room list + create form
               GET /downloads    the published release
               GET /room/<id>    room page (connect block, status, logs, controls)

  JSON API:    POST   /rooms              upload .archipelago → create room
               GET    /rooms              list rooms
               GET    /room/<id>          room status/info
               POST   /room/<id>/start    manual start
               POST   /room/<id>/stop     manual stop
               GET    /room/<id>/health   CPU/RSS + liveness probe
               GET    /room/<id>/logs     paged log tail (or SSE stream)
               DELETE /room/<id>          delete room + files
               POST   /room/<id>/password set/clear password

The orchestrator is injected via create_app(manager=...) so tests can pass a
mock without spawning real processes.
"""

from __future__ import annotations

import os
import time
import json
import logging
import threading

from flask import (
    Flask, request, redirect, url_for, render_template,
    jsonify, abort, Response, stream_with_context, send_from_directory,
)
from werkzeug.exceptions import NotFound

from webgui.orchestrator import (
    RoomManager, DEFAULT_IDLE_TIMEOUT, DEFAULT_UPLOAD_MAX_BYTES, DEFAULT_ROOM_MAX_AS_MB,
)
from webgui import generator
from webgui import releases

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config constants (override in environment or app.config)
# ---------------------------------------------------------------------------

DONATION_URL = os.environ.get(
    "DONATION_URL", "https://buymeacoffee.com/fazuzu"
)

# Contact details rendered into the header/footer of every page. A Discord handle is not a
# URL -- there is no profile link you can hand a stranger -- so the template renders it as
# copyable text rather than a dead <a href>.
CONTACT_DISCORD = os.environ.get("CONTACT_DISCORD", "@rickyquick")
CONTACT_GITHUB  = os.environ.get("CONTACT_GITHUB", "https://github.com/4laric/archipelago")
DEFAULT_PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "localhost")
DEFAULT_DATA_DIR    = os.environ.get("DATA_DIR",    os.path.join(os.path.dirname(__file__), "room_data"))
DEFAULT_STORE_PATH  = os.environ.get("STORE_PATH",  os.path.join(os.path.dirname(__file__), "rooms.json"))
DEFAULT_REPO_DIR    = os.environ.get("REPO_DIR",    os.path.dirname(os.path.dirname(__file__)))
DEFAULT_PORT_START  = int(os.environ.get("PORT_START", "38400"))
DEFAULT_PORT_END    = int(os.environ.get("PORT_END",   "38600"))
LOG_TAIL_LINES      = int(os.environ.get("LOG_TAIL_LINES", "200"))
ROOM_MAX_AS_MB      = int(os.environ.get("ROOM_MAX_AS_MB", str(DEFAULT_ROOM_MAX_AS_MB)))

# ---- the SSE log stream, which is the scarcest resource on this box ----------------------------
#
# 🛑 THREADS ARE THE CEILING AND IT FAILS SILENTLY. gunicorn runs ONE worker with 64 threads, and
# every open /room/<id>/logs?stream=1 parks one for the life of the connection. When they are all
# parked the whole site stops answering with no crash, no log line and the container still "Up":
# gunicorn's --timeout only fires on workers that stop heartbeating, and a gthread worker keeps
# heartbeating from its main loop while every request thread is blocked. This wedged the site for
# three weeks (2026-07-17 -> 2026-08-07) at the previous value of 8 threads.
#
# Raising the thread count bought headroom and fixed nothing: a browser tab left open overnight
# holds its thread overnight. Two caps close it properly, and they are different failures:
#
#   SSE_MAX_SECONDS  -- no single stream outlives this. The tab does not notice: EventSource
#                       reconnects on close by itself, and room.html retries on error anyway, so a
#                       recycled stream is invisible to a watching human and releases the thread of
#                       one who stopped watching.
#   SSE_MAX_STREAMS  -- a hard ceiling well below the thread count, so log viewers can never take
#                       the last thread away from the rest of the site. Past it the answer is a 503
#                       that says what to do, not a hang.
# 🛑 0 MEANS NO CAP, for an operator who decides this is wrong for their box -- and that escape
# hatch is exactly what hung the test suite on its first run, because `x if SSE_MAX_SECONDS else
# None` reads 0 as "unlimited" and a test that set 0 expecting "immediate" never got its generator
# back. Keep the hatch; do not test with it.
SSE_MAX_SECONDS     = int(os.environ.get("SSE_MAX_SECONDS", "900"))
SSE_MAX_STREAMS     = int(os.environ.get("SSE_MAX_STREAMS", "24"))

# Seed generation. AP_ROOT is the checkout generation runs in -- the pinned deploy tree with the
# apworlds installed. It defaults to the repo this app ships inside, which is right for the box and
# for the tests, and is overridable so a staging pin can be pointed at without a redeploy.
AP_ROOT             = os.environ.get("AP_ROOT", DEFAULT_REPO_DIR)
GENERATE_ENABLED    = os.environ.get("GENERATE_ENABLED", "1") not in ("0", "false", "False")
GENERATE_TIMEOUT    = int(os.environ.get("GENERATE_TIMEOUT", str(generator.DEFAULT_TIMEOUT_SECONDS)))
GENERATE_MAX_AS_MB  = int(os.environ.get("GENERATE_MAX_AS_MB", str(generator.DEFAULT_MAX_ADDRESS_SPACE_MB)))
GENERATE_PLANDO     = os.environ.get("GENERATE_PLANDO", generator.DEFAULT_PLANDO)

# Static tooling served under /er -- the Elden Ring options wizard and the check browser. They are
# single-file HTML pages built in the er-archipelago repo, NOT vendored here; a deploy copies them
# into ER_STATIC_DIR. Unset (or a missing dir) = the routes 404 and nothing else changes.
#
# 🛑 THIS DIRECTORY IS THE FRONT DOOR: `/` serves landing.html out of it. Four pages arrive
# through it -- landing.html, wizard.html, checks.html, report.html -- plus tabs.js, the one
# definition of the site's tab strip. Each is installed atomically and pinned to a release tag by
# er-archipelago's tools/deploy_wizard.sh.
#
# 🛑 SERVING THEM IS ALSO WHAT MAKES THE WIZARD'S "Generate & host" BUTTON POSSIBLE AT ALL, and
# that argument came back with the button (v0.4.1). It is same-origin only on purpose: from a
# file:// page the origin is `null`, so a cross-origin POST would either fail CORS or force
# Access-Control-Allow-Origin: * onto /generate -- the one endpoint that spends CPU on a
# stranger's input. Served from here it is a same-origin POST and no CORS policy exists to get
# wrong. The file:// copy inside every release zip therefore does NOT get the button; it gets a
# link to the hosted wizard.
ER_STATIC_DIR = os.environ.get("ER_STATIC_DIR", "")


# ---------------------------------------------------------------------------
# App factory (injectable manager for testing)
# ---------------------------------------------------------------------------

def create_app(manager: RoomManager = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.config["MAX_CONTENT_LENGTH"] = DEFAULT_UPLOAD_MAX_BYTES
    app.config["DONATION_URL"] = DONATION_URL
    app.config["PUBLIC_HOST"]  = DEFAULT_PUBLIC_HOST
    app.config["CONTACT_DISCORD"] = CONTACT_DISCORD
    app.config["CONTACT_GITHUB"]  = CONTACT_GITHUB

    @app.context_processor
    def site_chrome():
        """Header/footer values, injected into EVERY template.

        Deliberately not a kwarg on each render_template: three call sites already had to
        repeat donation_url, and a fourth that forgot it would render a footer with a dead
        link and an empty contact line, with nothing to fail. Chrome belongs to the layout,
        so it is supplied by the layout.
        """
        return {
            "donation_url":    app.config["DONATION_URL"],
            "contact_discord": app.config["CONTACT_DISCORD"],
            "contact_github":  app.config["CONTACT_GITHUB"],
            # The tab strip lives in base.html and four of its six tabs are served out of
            # ER_STATIC_DIR, so it has to know whether that directory exists -- a Builder tab
            # that 404s is worse than no Builder tab. Read at request time, not captured at
            # create_app time, so a test (and a deploy that lands mid-process) sees the truth.
            "er_tooling": bool(ER_STATIC_DIR and os.path.isdir(ER_STATIC_DIR)),
            # Older ER_REF values legitimately lack the optional questline artifact. Do not
            # advertise a tab whose target was not copied into this particular deployment.
            "questline_dag": bool(ER_STATIC_DIR and os.path.isfile(
                os.path.join(ER_STATIC_DIR, "questlines.html"))),
        }

    if manager is None:
        manager = RoomManager(
            data_dir=DEFAULT_DATA_DIR,
            store_path=DEFAULT_STORE_PATH,
            repo_dir=DEFAULT_REPO_DIR,
            public_host=DEFAULT_PUBLIC_HOST,
            port_start=DEFAULT_PORT_START,
            port_end=DEFAULT_PORT_END,
            room_max_as_mb=ROOM_MAX_AS_MB,
        )
        manager.start_idle_reaper()

    app.extensions["room_manager"] = manager
    # Live count of open SSE log streams. One process, so a plain int under a lock is the whole
    # mechanism -- and it must stay that way: a second gunicorn worker would give each its own
    # counter and its own share of threads, which is a different design, not a bigger number.
    app.extensions["sse_streams"] = 0
    app.extensions["sse_lock"] = threading.Lock()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def mgr() -> RoomManager:
        return app.extensions["room_manager"]

    def room_summary(room) -> dict:
        ci = room.connect_info(mgr().public_host)
        return {
            "id":           room.id,
            "name":         room.name,
            "status":       room.status,
            "tier":         room.tier,
            "port":         room.port,
            "connect":      ci,
            "password_set": bool(room.password),
            "created_at":   room.created_at,
            "last_active_at": room.last_active_at,
            "crash_count":  room.crash_count,
            "idle_timeout": room.idle_timeout,
        }

    def _wants_json() -> bool:
        return request.accept_mimetypes.best == "application/json" or \
               request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # ------------------------------------------------------------------
    # HTML pages
    # ------------------------------------------------------------------

    @app.route("/")
    def front_door():
        """The front door is the Elden Ring landing page, served out of ER_STATIC_DIR.

        🛑 IT IS A STATIC FILE, NOT A TEMPLATE, ON PURPOSE. `landing.html` is built and gated in
        the er-archipelago repo and installed here by `tools/deploy_wizard.sh --landing`, which
        fetches it AT THE STABLE TAG and writes it atomically -- exactly how wizard.html and
        checks.html arrive, and for the same reason. Porting it into `templates/` would make it a
        fourth surface that drifts from the release, which is the failure
        SPEC-publishing-pipeline.md was written about.

        It used to be called `dashboard`, which by v0.4.0 named the opposite of what it served.
        The rooms dashboard is `/hosting` and the view is `hosting()`; this one is `front_door`.
        """
        if ER_STATIC_DIR and os.path.isfile(os.path.join(ER_STATIC_DIR, "landing.html")):
            return send_from_directory(ER_STATIC_DIR, "landing.html")
        # Rule 2: an empty result is a failure, not a clean run. A blank front page on a box that
        # has not been deployed to should say which command was not run, not 404 as though the
        # site does not exist.
        return (
            "<h1>Elden Ring for Archipelago</h1>"
            "<p>No landing page deployed on this host. Run "
            "<code>ER_STATIC_DIR=... tools/deploy_wizard.sh --landing</code> from the "
            "er-archipelago repo.</p>"
            "<p><a href=\"/er/\">Options wizard</a> &middot; "
            "<a href=\"/downloads\">Downloads</a> &middot; "
            "<a href=\"/hosting\">Hosting</a></p>"
        ), 503

    @app.route("/hosting")
    def hosting():
        """The rooms dashboard -- one tab of the site, and deliberately not the front page.

        The builder is what people arrive for; it is the only surface anyone can use before
        deciding whether to install a DLL. Hosting sits beside it rather than in front of it,
        which is also why this is `/hosting` and not `/`.

        `can_generate` and `er_tooling` are asked, not assumed: a link to /er/ on a box with no ER
        tooling deployed is a 404 with extra steps, and "hosting only, upload a seed you generated
        elsewhere" is the honest copy on a box that cannot generate. The template renders whichever
        of those two sites this actually is.
        """
        return render_template(
            "index.html",
            tab="hosting",
            rooms=mgr().list_rooms(),
            public_host=mgr().public_host,
            can_generate=bool(GENERATE_ENABLED and AP_ROOT and os.path.isfile(
                os.path.join(AP_ROOT, "Generate.py"))),
            # er_tooling arrives from the context processor -- it is chrome, and this page is not
            # the only one that needs it.
        )

    @app.route("/downloads")
    def downloads():
        """The published Elden Ring release: what to download and in what order.

        Peliarch hosts rooms; er-archipelago publishes the game. Those are different repos with
        different release cadences, so this page reads the release rather than restating it --
        see `webgui/releases.py` for why a hardcoded version here would be a fourth surface to
        forget to bump.
        """
        return render_template(
            "downloads.html",
            tab="downloads",
            rel=releases.get_releases(),
            dev=releases.get_dev_release(),
            channels_url=releases.CHANNELS_URL,
            nexus_url=releases.NEXUS_URL,
            game_github_url=releases.GAME_GITHUB_URL,
            releases_index=releases.GAME_GITHUB_URL.rstrip("/") + "/releases",
            er_tooling=bool(ER_STATIC_DIR and os.path.isdir(ER_STATIC_DIR)),
        )

    @app.route("/room/<room_id>")
    def room_page(room_id):
        room = mgr().get_room(room_id)
        if room is None:
            if _wants_json():
                return jsonify(error="not found"), 404
            abort(404)
        if _wants_json():
            return jsonify(room_summary(room))
        logs = mgr().log_tail(room_id, lines=LOG_TAIL_LINES)
        host = mgr().public_host
        ci   = room.connect_info(host)
        return render_template(
            "room.html",
            tab="hosting",
            room=room,
            connect=ci,
            logs=logs,
            public_host=host,
        )

    # ------------------------------------------------------------------
    # API: rooms collection
    # ------------------------------------------------------------------

    @app.route("/er/")
    @app.route("/er/<path:filename>")
    def er_static(filename: str = "wizard.html"):
        """Serve the Elden Ring wizard / check browser from ER_STATIC_DIR.

        `send_from_directory` resolves against the base and refuses to escape it, so `..` in the URL
        is handled by Flask rather than by a hand-rolled check here.
        """
        if not ER_STATIC_DIR or not os.path.isdir(ER_STATIC_DIR):
            return jsonify(error="No ER tooling deployed on this host (set ER_STATIC_DIR)"), 404
        try:
            return send_from_directory(ER_STATIC_DIR, filename)
        except NotFound:
            return jsonify(error=f"No such file: {filename}"), 404

    # ------------------------------------------------------------------
    # Room creation and seed generation. RETIRED AT v0.4.0, BACK AT v0.4.1 WITH THE DEFECT FIXED.
    #
    # 🛑 READ THE MOTIVATING CASE BEFORE CHANGING ANYTHING HERE (rule 11), 2026-08-12: the
    # dashboard offered five hibernated rooms the SAME connect address, ws://host:38400, with a
    # Copy button beside each one. Archipelago's Connect packet carries a slot name and a password
    # and NO room identifier, so a client reaching whichever server actually held 38400 -- with a
    # slot name that seed happened to contain -- would join the wrong multiworld and be told
    # nothing by either side. Two of those rooms were both named "Player - Elden Ring", so the name
    # did not distinguish them either. Hosting was scoped out rather than patched, a week before
    # the first public announcement, because nobody owned that failure mode.
    #
    # ⭐ THE DIAGNOSIS IN THAT NOTE WAS WRONG IN ITS DETAIL AND IT MATTERS. It blamed
    # RandomPortSocketCreator taking a port at socket-creation time. This app does not use that:
    # `_launch_multiserver` passes `--port` explicitly, so a RUNNING room's port was always
    # truthful. The lie was in the record -- `start_room` allocated per START via `free_port`, and
    # `stop_room` never cleared `port` -- so five sleeping rooms each remembered the 38400 they
    # last held, and the next one to wake took it for real.
    #
    # WHAT MAKES IT SAFE NOW, both in webgui/orchestrator.py and both under test in
    # webgui/test_ports.py:
    #   1. A port is allocated ONCE, at room creation, excluding every port the store has already
    #      promised, and the room keeps it for its whole life. A stale address a player copied last
    #      week resolves to that room or to nothing -- never to somebody else's seed.
    #   2. `Room.connect_info` returns an address only while `status == RUNNING`. A port number is
    #      not an address, and a sleeping room now says so in words.
    # `_resolve_port_collisions` re-homes the rooms already on the box, which all record 38400.
    #
    # 🛑 THE 410 BODY IS GONE BUT ITS AUDIENCE IS NOT. Two callers we cannot update still POST
    # here: an options wizard already open in somebody's browser, and the file:// wizard inside
    # every previous release zip. Both render `data.error` verbatim into their own UI, so every
    # failure reply below has to be a sentence a player can act on -- not a bare status code.
    # ------------------------------------------------------------------

    @app.route("/rooms", methods=["POST"])
    def create_room():
        """Upload .archipelago and create a room."""
        if "file" not in request.files:
            return jsonify(error="No file uploaded (field name: 'file')"), 400

        f = request.files["file"]
        if not f.filename:
            return jsonify(error="Empty filename"), 400

        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in (".archipelago", ".zip"):
            return jsonify(error="File must be .archipelago or .zip"), 400

        name         = (request.form.get("name") or f.filename).strip()
        password     = request.form.get("password") or None
        tier         = request.form.get("tier", "Standard")
        idle_timeout = int(request.form.get("idle_timeout", DEFAULT_IDLE_TIMEOUT))

        data = f.read()
        try:
            room = mgr().create_room(
                name=name,
                file_data=data,
                filename=f.filename,
                password=password,
                idle_timeout=idle_timeout,
                tier=tier,
            )
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except Exception as e:
            logger.exception("create_room failed")
            return jsonify(error=str(e)), 500

        # Auto-start on create
        try:
            room = mgr().start_room(room.id)
        except Exception as e:
            logger.warning("Auto-start failed for room %s: %s", room.id, e)

        if _wants_json():
            return jsonify(room_summary(room)), 201
        return redirect(url_for("room_page", room_id=room.id))

    @app.route("/generate", methods=["POST"])
    def generate_and_host():
        """yaml in, running room out -- the stage that used to happen on the player's own machine.

        Accepts either a `yaml` form field (what the options wizard POSTs) or one-or-more uploaded
        `file` parts (a multiworld's player files). On success it does exactly what /rooms does with
        an upload, because the generated seed IS an upload as far as the orchestrator is concerned:
        create the room, auto-start it, hand back the connect address.

        🛑 This is the one endpoint on the site that runs a program over text a stranger wrote.
        webgui/generator.py holds that line -- wall timeout, RLIMIT_AS/CPU, process-group kill,
        plando off -- and it can bound a SINGLE generation but not the ARRIVAL RATE. That half is
        the `rate_limit` block in deploy/docker/Caddyfile, which came back with this route and
        must not be dropped again: one gunicorn worker means an in-app limiter competes for the
        very thread it is trying to protect. Do not move generation in-process to save a fork;
        the fork IS the boundary.
        """
        if not GENERATE_ENABLED:
            return jsonify(error="Seed generation is disabled on this host"), 503

        yamls = []
        for f in request.files.getlist("file"):
            if f.filename:
                yamls.append(f.read())
        text = request.form.get("yaml")
        if text:
            yamls.append(text.encode("utf-8"))
        if not yamls:
            return jsonify(error="No yaml supplied (form field 'yaml', or file parts named 'file')"), 400

        try:
            generator.validate_yamls(yamls)
        except ValueError as e:
            return jsonify(error=str(e)), 400

        seed_arg = request.form.get("seed")
        try:
            result = generator.generate(
                yamls, AP_ROOT,
                seed=int(seed_arg) if seed_arg else None,
                plando=GENERATE_PLANDO,
                timeout=GENERATE_TIMEOUT,
                max_address_space_mb=GENERATE_MAX_AS_MB,
            )
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except generator.GenerationError as e:
            # 504 for "it ran too long", 422 for "your yaml does not generate" -- a caller needs to
            # tell "try again" apart from "fix your options", and both are the user's fault, not a 500.
            code = 504 if e.timed_out else 422
            return jsonify(error=str(e), detail=e.detail), code
        except Exception as e:
            logger.exception("generate failed")
            return jsonify(error=str(e)), 500

        name         = (request.form.get("name") or result.filename).strip()
        password     = request.form.get("password") or None
        tier         = request.form.get("tier", "Standard")
        idle_timeout = int(request.form.get("idle_timeout", DEFAULT_IDLE_TIMEOUT))

        try:
            room = mgr().create_room(
                name=name, file_data=result.data, filename=result.filename,
                password=password, idle_timeout=idle_timeout, tier=tier,
            )
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except Exception as e:
            logger.exception("create_room after generate failed")
            return jsonify(error=str(e)), 500

        try:
            room = mgr().start_room(room.id)
        except Exception as e:
            logger.warning("Auto-start failed for generated room %s: %s", room.id, e)

        if _wants_json():
            body = room_summary(room)
            body["seed_file"] = result.filename
            return jsonify(body), 201
        return redirect(url_for("room_page", room_id=room.id))

    @app.route("/rooms", methods=["GET"])
    def list_rooms():
        rooms = mgr().list_rooms()
        if _wants_json():
            return jsonify([room_summary(r) for r in rooms])
        return redirect(url_for("hosting"))

    # ------------------------------------------------------------------
    # API: single room
    # ------------------------------------------------------------------

    @app.route("/room/<room_id>/start", methods=["POST"])
    def start_room(room_id):
        room = mgr().get_room(room_id)
        if room is None:
            return jsonify(error="not found"), 404
        try:
            room = mgr().start_room(room_id)
        except Exception as e:
            logger.exception("start_room %s failed", room_id)
            return jsonify(error=str(e)), 500
        if _wants_json():
            return jsonify(room_summary(room))
        return redirect(url_for("room_page", room_id=room_id))

    @app.route("/room/<room_id>/stop", methods=["POST"])
    def stop_room(room_id):
        room = mgr().get_room(room_id)
        if room is None:
            return jsonify(error="not found"), 404
        try:
            room = mgr().stop_room(room_id)
        except Exception as e:
            logger.exception("stop_room %s failed", room_id)
            return jsonify(error=str(e)), 500
        if _wants_json():
            return jsonify(room_summary(room))
        return redirect(url_for("room_page", room_id=room_id))

    @app.route("/room/<room_id>/health", methods=["GET"])
    def room_health(room_id):
        h = mgr().health(room_id)
        if "error" in h:
            return jsonify(h), 404
        return jsonify(h)

    @app.route("/room/<room_id>/logs", methods=["GET"])
    def room_logs(room_id):
        room = mgr().get_room(room_id)
        if room is None:
            return jsonify(error="not found"), 404

        stream = request.args.get("stream", "0") == "1"

        if stream:
            # Claim a slot BEFORE building the response -- check and increment under one lock, so
            # two simultaneous viewers cannot both pass a ceiling that only has room for one.
            #
            # 🛑 RELEASED FROM TWO PLACES, IDEMPOTENTLY, BECAUSE NEITHER ALONE IS ENOUGH, and a
            # leaked slot is permanent -- worse than the hang this exists to prevent.
            #   * the generator's `finally` covers the normal life of a stream: exhausted by the
            #     deadline, or GeneratorExit when the browser goes away.
            #   * `call_on_close` covers a response that is closed before the generator is ever
            #     iterated, where the body is never entered and no `finally` runs.
            # The first is the one the tests exercise; the second is the one a real WSGI server
            # exercises. `_released` makes running both harmless.
            with app.extensions["sse_lock"]:
                if app.extensions["sse_streams"] >= SSE_MAX_STREAMS:
                    return jsonify(
                        error=(
                            f"Too many log viewers open ({SSE_MAX_STREAMS}). Close a room page "
                            f"and try again; the log tail below still works."
                        ),
                    ), 503
                app.extensions["sse_streams"] += 1

            _released = {"done": False}

            def release():
                with app.extensions["sse_lock"]:
                    if not _released["done"]:
                        _released["done"] = True
                        app.extensions["sse_streams"] -= 1

            # SSE: tail the log file and stream new lines
            def generate():
                import time as _time
                try:
                    last_pos = 0
                    log_path = room.log_path
                    deadline = _time.time() + SSE_MAX_SECONDS if SSE_MAX_SECONDS else None
                    while deadline is None or _time.time() < deadline:
                        if log_path and os.path.exists(log_path):
                            with open(log_path) as lf:
                                lf.seek(last_pos)
                                new = lf.read()
                                last_pos = lf.tell()
                            if new:
                                for line in new.splitlines():
                                    yield f"data: {json.dumps(line)}\n\n"
                        else:
                            yield ": keepalive\n\n"
                        _time.sleep(1)
                    # Ending the stream is not an error and does not need announcing to the user:
                    # the browser's EventSource reconnects on close by itself. The comment line is
                    # for whoever reads a capture and wonders why the stream stopped.
                    yield ": stream recycled after SSE_MAX_SECONDS; reconnecting\n\n"
                finally:
                    release()

            resp = Response(
                stream_with_context(generate()),
                content_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
            resp.call_on_close(release)
            return resp

        lines = int(request.args.get("lines", LOG_TAIL_LINES))
        return jsonify(lines=mgr().log_tail(room_id, lines=lines))

    @app.route("/room/<room_id>", methods=["DELETE"])
    def delete_room(room_id):
        room = mgr().get_room(room_id)
        if room is None:
            return jsonify(error="not found"), 404
        try:
            mgr().delete_room(room_id)
        except Exception as e:
            logger.exception("delete_room %s failed", room_id)
            return jsonify(error=str(e)), 500
        return jsonify(ok=True)

    @app.route("/room/<room_id>/password", methods=["POST"])
    def set_password(room_id):
        room = mgr().get_room(room_id)
        if room is None:
            return jsonify(error="not found"), 404
        password = request.json.get("password") if request.is_json else request.form.get("password")
        try:
            room = mgr().set_password(room_id, password or None)
        except Exception as e:
            return jsonify(error=str(e)), 500
        if _wants_json():
            return jsonify(room_summary(room))
        return redirect(url_for("room_page", room_id=room_id))

    # ------------------------------------------------------------------
    # Error handlers
    # ------------------------------------------------------------------

    @app.errorhandler(404)
    def not_found(e):
        if _wants_json():
            return jsonify(error="not found"), 404
        return render_template(
            "base.html",
            page_title="Not Found",
            content="<p>Room or page not found.</p>",
        ), 404

    @app.errorhandler(413)
    def too_large(e):
        if _wants_json():
            return jsonify(error=f"Upload too large (max {DEFAULT_UPLOAD_MAX_BYTES // (1024*1024)} MB)"), 413
        return f"Upload too large (max {DEFAULT_UPLOAD_MAX_BYTES // (1024*1024)} MB)", 413

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    application = create_app()
    application.run(host="0.0.0.0", port=8080, debug=False)
