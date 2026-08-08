"""
app.py — Flask web app for Peliarch self-host GUI.

Implements:
  HTML pages:  GET /              dashboard
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

from flask import (
    Flask, request, redirect, url_for, render_template,
    jsonify, abort, Response, stream_with_context,
)

from webgui.orchestrator import RoomManager, DEFAULT_IDLE_TIMEOUT, DEFAULT_UPLOAD_MAX_BYTES
from webgui import generator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config constants (override in environment or app.config)
# ---------------------------------------------------------------------------

DONATION_URL = os.environ.get(
    "DONATION_URL", "https://buymeacoffee.com/your-handle"
)
DEFAULT_PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "localhost")
DEFAULT_DATA_DIR    = os.environ.get("DATA_DIR",    os.path.join(os.path.dirname(__file__), "room_data"))
DEFAULT_STORE_PATH  = os.environ.get("STORE_PATH",  os.path.join(os.path.dirname(__file__), "rooms.json"))
DEFAULT_REPO_DIR    = os.environ.get("REPO_DIR",    os.path.dirname(os.path.dirname(__file__)))
DEFAULT_PORT_START  = int(os.environ.get("PORT_START", "38400"))
DEFAULT_PORT_END    = int(os.environ.get("PORT_END",   "38600"))
LOG_TAIL_LINES      = int(os.environ.get("LOG_TAIL_LINES", "200"))

# Seed generation. AP_ROOT is the checkout generation runs in -- the pinned deploy tree with the
# apworlds installed. It defaults to the repo this app ships inside, which is right for the box and
# for the tests, and is overridable so a staging pin can be pointed at without a redeploy.
AP_ROOT             = os.environ.get("AP_ROOT", DEFAULT_REPO_DIR)
GENERATE_ENABLED    = os.environ.get("GENERATE_ENABLED", "1") not in ("0", "false", "False")
GENERATE_TIMEOUT    = int(os.environ.get("GENERATE_TIMEOUT", str(generator.DEFAULT_TIMEOUT_SECONDS)))
GENERATE_MAX_AS_MB  = int(os.environ.get("GENERATE_MAX_AS_MB", str(generator.DEFAULT_MAX_ADDRESS_SPACE_MB)))
GENERATE_PLANDO     = os.environ.get("GENERATE_PLANDO", generator.DEFAULT_PLANDO)


# ---------------------------------------------------------------------------
# App factory (injectable manager for testing)
# ---------------------------------------------------------------------------

def create_app(manager: RoomManager = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.config["MAX_CONTENT_LENGTH"] = DEFAULT_UPLOAD_MAX_BYTES
    app.config["DONATION_URL"] = DONATION_URL
    app.config["PUBLIC_HOST"]  = DEFAULT_PUBLIC_HOST

    if manager is None:
        manager = RoomManager(
            data_dir=DEFAULT_DATA_DIR,
            store_path=DEFAULT_STORE_PATH,
            repo_dir=DEFAULT_REPO_DIR,
            public_host=DEFAULT_PUBLIC_HOST,
            port_start=DEFAULT_PORT_START,
            port_end=DEFAULT_PORT_END,
        )
        manager.start_idle_reaper()

    app.extensions["room_manager"] = manager

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
    def dashboard():
        rooms = mgr().list_rooms()
        host  = mgr().public_host
        return render_template(
            "index.html",
            rooms=rooms,
            public_host=host,
            donation_url=app.config["DONATION_URL"],
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
            room=room,
            connect=ci,
            logs=logs,
            public_host=host,
            donation_url=app.config["DONATION_URL"],
        )

    # ------------------------------------------------------------------
    # API: rooms collection
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
        plando off. Do not move generation in-process to save a fork; the fork IS the boundary.
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
        return redirect(url_for("dashboard"))

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
            # SSE: tail the log file and stream new lines
            def generate():
                import time as _time
                last_pos = 0
                log_path = room.log_path
                while True:
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

            return Response(
                stream_with_context(generate()),
                content_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

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
            donation_url=app.config["DONATION_URL"],
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
