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
    jsonify, abort, Response, stream_with_context, send_from_directory,
)
from werkzeug.exceptions import NotFound

from webgui.orchestrator import RoomManager, DEFAULT_IDLE_TIMEOUT, DEFAULT_UPLOAD_MAX_BYTES
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
# 🛑 THIS DIRECTORY IS ALSO THE FRONT DOOR AS OF v0.4.0: `/` serves landing.html out of it.
#
# It used to be justified by the wizard's "Generate & host" button, which was same-origin only so
# that a file:// page's `null` origin could not force Access-Control-Allow-Origin: * onto
# /generate. That button and that endpoint are both retired, so the CORS argument is gone -- and
# the directory matters MORE, not less, because three separate pages now arrive through it:
# landing.html, wizard.html and checks.html, each installed atomically and each pinned to a
# release tag by er-archipelago's tools/deploy_wizard.sh.
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
        }

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
        """The front door is the Elden Ring landing page, served out of ER_STATIC_DIR.

        🛑 IT IS A STATIC FILE, NOT A TEMPLATE, ON PURPOSE. `landing.html` is built and gated in
        the er-archipelago repo and installed here by `tools/deploy_wizard.sh --landing`, which
        fetches it AT THE STABLE TAG and writes it atomically -- exactly how wizard.html and
        checks.html arrive, and for the same reason. Porting it into `templates/` would make it a
        fourth surface that drifts from the release, which is the failure
        SPEC-publishing-pipeline.md was written about.

        The name `dashboard` is kept because `url_for("dashboard")` is referenced elsewhere;
        renaming it is a follow-up, not part of scoping hosting out.
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
            "<a href=\"/downloads\">Downloads</a></p>"
        ), 503

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
            rel=releases.get_releases(),
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
    # RETIRED AT v0.4.0: room creation and seed generation
    #
    # 🛑 410 GONE, NOT A DELETED ROUTE, AND NOT A 404. Two callers still exist in the wild and
    # neither can be updated by us: an options wizard already open in somebody's browser, and the
    # file:// copy of an older wizard shipped inside every previous release zip. Both POST here.
    # The old wizard renders `data.error` straight into its own UI, so a 410 carrying a readable
    # sentence is the only way to tell a player what happened -- a 404 reads as "the site is
    # broken" and a deleted route reads as nothing at all.
    #
    # WHY: hosting is out of scope for this project as of v0.4.0. The site is the yaml builder,
    # the downloads, the documentation, the check browser and the bug report form.
    #
    # THE MOTIVATING CASE (rule 11), 2026-08-12: the dashboard offered five hibernated rooms the
    # SAME connect address, ws://host:38400, with a Copy button beside it. The allocator is
    # genuinely port-per-room -- RandomPortSocketCreator takes a free port out of 38400-38463 when
    # the socket is created -- so 38400 was a placeholder for rooms with no live socket, and four
    # of the five were wrong the moment their room woke. Archipelago's Connect packet carries a
    # slot name and a password and NO room identifier, so a client reaching whichever server
    # actually held 38400, with a slot name that seed happened to contain, would join the wrong
    # multiworld and be told nothing. Two of those rooms were both named "Player - Elden Ring".
    #
    # The display bug is an afternoon. Owning the failure mode is not, a week before the first
    # public announcement, so the surface is gone rather than patched.
    #
    # EXISTING ROOMS ARE NOT TOUCHED. /room/<id> and its start/stop/logs/delete routes stay so
    # nobody loses a seed mid-run. Removing those is a v0.4.1 job, once they have aged out.
    # ------------------------------------------------------------------

    _RETIRED = (
        "Peliarch no longer hosts rooms or generates seeds. Use the options wizard to build "
        "your yaml, then generate with Archipelago locally or host at archipelago.gg. Your "
        "existing rooms still work."
    )

    @app.route("/rooms", methods=["POST"])
    def create_room():
        return jsonify(error=_RETIRED, retired=True), 410

    @app.route("/generate", methods=["POST"])
    def generate_and_host():
        return jsonify(error=_RETIRED, retired=True), 410

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
