"""
test_app.py — Flask test-client tests for the Peliarch web GUI.

Runs entirely via Flask's synchronous test client — no long-running server,
no real MultiServer processes.  The orchestrator is replaced with a MockManager
that tracks calls without spawning anything.

Run:
    cd webgui
    python -m pytest test_app.py -v
  or (from repo root):
    python -m pytest webgui/test_app.py -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile

import pytest

# Allow importing webgui as a package whether run from repo root or webgui/
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

from webgui.orchestrator import Room, validate_archipelago_upload
from webgui import app as app_module
from webgui.app import create_app, DONATION_URL, CONTACT_DISCORD, CONTACT_GITHUB


# ---------------------------------------------------------------------------
# Helpers: build a minimal valid .archipelago (zip) for upload tests
# ---------------------------------------------------------------------------

def _make_archipelago(size_bytes: int = 1024) -> bytes:
    """Return a minimal valid .archipelago (zip with one entry)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("multidata", b"x" * size_bytes)
    return buf.getvalue()


def _make_oversized_archipelago(max_bytes: int) -> bytes:
    """Return a zip that exceeds max_bytes raw."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        # Store uncompressed so the zip size ≈ payload size
        zf.writestr("multidata", b"x" * (max_bytes + 1))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# MockManager (injectable orchestrator — no real processes)
# ---------------------------------------------------------------------------

class MockManager:
    """Drop-in replacement for RoomManager used in tests."""

    DEFAULT_HOST = "testhost"

    def __init__(self):
        self._rooms: dict[str, Room] = {}
        self.public_host = self.DEFAULT_HOST
        self.upload_max_bytes = 64 * 1024 * 1024
        self._calls: list[tuple] = []  # log of (method, *args)

    def _log(self, method, *args):
        self._calls.append((method,) + args)

    # -- Rooms --

    def create_room(self, name, file_data, filename, password=None,
                    idle_timeout=1800, tier="Standard") -> Room:
        self._log("create_room", name)
        # Re-run real validation so upload-rejection tests work
        validate_archipelago_upload(file_data, self.upload_max_bytes)
        import uuid
        room_id = str(uuid.uuid4())[:8]
        room = Room(
            id=room_id,
            name=name,
            multidata_path=f"/mock/{room_id}/multidata.archipelago",
            save_path=f"/mock/{room_id}/save.apsave",
            password=password,
            idle_timeout=idle_timeout,
            tier=tier,
            port=38401,   # fixed mock port
            status="UPLOADED",
            log_path=None,
        )
        self._rooms[room_id] = room
        return room

    def list_rooms(self):
        return list(self._rooms.values())

    def get_room(self, room_id):
        return self._rooms.get(room_id)

    def start_room(self, room_id) -> Room:
        self._log("start_room", room_id)
        room = self._rooms.get(room_id)
        if room is None:
            raise KeyError(room_id)
        room.status = "RUNNING"
        room.port = 38401
        return room

    def stop_room(self, room_id) -> Room:
        self._log("stop_room", room_id)
        room = self._rooms.get(room_id)
        if room is None:
            raise KeyError(room_id)
        room.status = "HIBERNATED"
        return room

    def delete_room(self, room_id) -> None:
        self._log("delete_room", room_id)
        self._rooms.pop(room_id, None)

    def set_password(self, room_id, password) -> Room:
        self._log("set_password", room_id, password)
        room = self._rooms.get(room_id)
        if room is None:
            raise KeyError(room_id)
        room.password = password
        return room

    def health(self, room_id) -> dict:
        room = self._rooms.get(room_id)
        if room is None:
            return {"error": "not found"}
        return {
            "id": room_id,
            "status": room.status,
            "port": room.port,
            "pid": 12345,
            "alive": room.status == "RUNNING",
            "cpu_percent": 5.0,
            "rss_mb": 64.0,
        }

    def log_tail(self, room_id, lines=100):
        return ["[mock] server started", "[mock] player connected"]

    def touch_activity(self, room_id):
        pass

    def start_idle_reaper(self):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mgr():
    return MockManager()


@pytest.fixture
def app(mgr):
    application = create_app(manager=mgr)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def room_id(mgr):
    """An existing room, created through the MANAGER rather than through POST /rooms.

    🛑 IT WENT THROUGH THE ROUTE UNTIL v0.4.0, and that route is now 410 Gone -- room creation
    was retired when hosting was scoped out. Every start/stop/logs/delete test depends on this
    fixture, so leaving it pointed at the retired route would have turned "we stopped offering
    room creation" into "the whole room suite fails", which says nothing about whether EXISTING
    rooms still work -- and existing rooms still working is the entire point of retiring the
    creation path rather than deleting the room routes.
    """
    room = mgr.create_room(
        name="Test Room",
        file_data=_make_archipelago(),
        filename="test.archipelago",
    )
    return room.id


# ---------------------------------------------------------------------------
# Test: validation helpers
# ---------------------------------------------------------------------------

class TestValidation:

    def test_valid_upload(self):
        data = _make_archipelago(512)
        # Should not raise
        validate_archipelago_upload(data)

    def test_rejects_too_large(self):
        cap = 1024
        data = b"x" * (cap + 1)
        with pytest.raises(ValueError, match="too large"):
            validate_archipelago_upload(data, max_bytes=cap)

    def test_rejects_not_a_zip(self):
        with pytest.raises(ValueError, match="not a valid ZIP"):
            validate_archipelago_upload(b"not a zip at all", max_bytes=1024*1024)

    def test_rejects_empty_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass  # empty
        with pytest.raises(ValueError, match="empty"):
            validate_archipelago_upload(buf.getvalue())

    def test_rejects_path_traversal(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../evil.py", b"bad")
        with pytest.raises(ValueError, match="unsafe path"):
            validate_archipelago_upload(buf.getvalue())


# ---------------------------------------------------------------------------
# Test: POST /rooms (upload) -- RETIRED AT v0.4.0
#
# `TestCreateRoom` drove the upload path through the route: 201 + connect info on success, and
# 400/413 on a wrong extension, an oversize file, or no file at all.
#
# 🛑 THE VALIDATION THOSE TESTS EXERCISED IS NOT GONE AND IS STILL TESTED. It lives in
# `orchestrator.validate_archipelago_upload`, and `TestValidation` above drives it DIRECTLY --
# which is the stronger test anyway: it asserts the validator, not the validator-as-seen-through-
# a-route that no longer exists. What was lost with the route is only the wiring assertion, and
# there is nothing left for it to be wired to.
#
# The route's own contract now -- 410, with a body the OLD wizard can print -- is in
# TestRetiredRoutes below.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test: GET /rooms
# ---------------------------------------------------------------------------

class TestListRooms:

    def test_empty_list(self, client):
        resp = client.get("/rooms", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_list_after_create(self, client, room_id):
        resp = client.get("/rooms", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        rooms = resp.get_json()
        assert any(r["id"] == room_id for r in rooms)


# ---------------------------------------------------------------------------
# Test: GET /room/<id>
# ---------------------------------------------------------------------------

class TestGetRoom:

    def test_get_existing_room_json(self, client, room_id):
        resp = client.get(f"/room/{room_id}", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["id"] == room_id

    def test_get_nonexistent_room(self, client):
        resp = client.get("/room/deadbeef", headers={"Accept": "application/json"})
        assert resp.status_code == 404

    def test_room_page_html_contains_connect_address(self, client, room_id):
        """The HTML room page must render the wss:// connect address."""
        resp = client.get(f"/room/{room_id}")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "wss://" in html, "Room page must contain wss:// connect address"

    def test_room_page_html_contains_host_and_port(self, client, room_id):
        resp = client.get(f"/room/{room_id}")
        html = resp.data.decode()
        assert MockManager.DEFAULT_HOST in html, "Room page must show public_host"
        # Port 38401 is the mock port
        assert "38401" in html, "Room page must show the game port"

    def test_room_page_html_contains_donation_link(self, client, room_id):
        """The donation link must be present on the room page."""
        resp = client.get(f"/room/{room_id}")
        html = resp.data.decode()
        assert DONATION_URL in html, "Room page must contain the donation URL"
        assert "coffee" in html.lower(), "Room page must reference 'coffee' in donation text"


# ---------------------------------------------------------------------------
# Test: dashboard HTML
# ---------------------------------------------------------------------------

class TestFrontDoor:
    """`/` is the Elden Ring landing page as of v0.4.0, not the rooms dashboard.

    🛑 THE OLD TESTS HERE ASSERTED THE DASHBOARD RENDERED ROOM IDS. Deleting them without
    replacement would have left the front door with no test at all, which is the failure the
    old `test_dashboard_loads` docstring was itself about: it asserted "GoArchipelago" for weeks
    after the product was renamed and nobody noticed, because a permanently-red test trains
    everyone to read red as normal. A silently-absent test is the same lesson without the red.

    The page itself is NOT vendored here -- it is built and gated in er-archipelago and installed
    into ER_STATIC_DIR by `deploy_wizard.sh --landing`. So what is testable from this repo is the
    ROUTING contract, and that is what these assert.
    """

    def test_front_door_serves_the_landing_file_when_deployed(self, mgr, tmp_path, monkeypatch):
        monkeypatch.setattr(app_module, "ER_STATIC_DIR", str(tmp_path))
        (tmp_path / "landing.html").write_text("<h1>Elden Ring for Archipelago</h1>", encoding="utf-8")
        c = create_app(manager=mgr).test_client()
        resp = c.get("/")
        assert resp.status_code == 200
        assert "Elden Ring for Archipelago" in resp.data.decode()

    def test_front_door_says_which_command_was_not_run_when_undeployed(self, mgr, monkeypatch):
        """Rule 2: an empty result is a failure, not a clean run.

        A box with no landing page deployed must not answer 200-with-nothing or 404. It says the
        deploy step was not run and names it, because the person seeing this is the operator.
        """
        monkeypatch.setattr(app_module, "ER_STATIC_DIR", "")
        c = create_app(manager=mgr).test_client()
        resp = c.get("/")
        assert resp.status_code == 503
        assert "deploy_wizard.sh --landing" in resp.data.decode()

    def test_the_front_door_no_longer_lists_rooms(self, mgr, tmp_path, monkeypatch):
        """The room whose id used to be REQUIRED on this page must now be absent from it.

        This is the retirement, asserted from the outside: hosting is not advertised. The room
        itself still exists and its own page still works -- TestStartStop covers that.
        """
        monkeypatch.setattr(app_module, "ER_STATIC_DIR", str(tmp_path))
        (tmp_path / "landing.html").write_text("<h1>Elden Ring for Archipelago</h1>", encoding="utf-8")
        room = mgr.create_room(name="Secret Room", file_data=_make_archipelago(),
                               filename="test.archipelago")
        c = create_app(manager=mgr).test_client()
        html = c.get("/").data.decode()
        assert room.id not in html


class TestRetiredRoutes:
    """Room creation and seed generation answer 410 GONE, not 404 and not a deleted route.

    🛑 TWO CALLERS EXIST IN THE WILD AND NEITHER CAN BE UPDATED BY US: an options wizard already
    open in a browser, and the file:// wizard inside every previous release zip. Both POST here.
    The old wizard renders `data.error` straight into its own UI, so the body has to carry a
    readable sentence -- a bare 410 is a blank message where a player needed an instruction.
    """

    def test_room_creation_is_gone_and_says_why(self, client):
        resp = client.post("/rooms", data={"name": "x"},
                           content_type="multipart/form-data",
                           headers={"Accept": "application/json"})
        assert resp.status_code == 410
        assert "archipelago.gg" in resp.get_json()["error"]

    def test_generate_is_gone_and_says_why(self, client):
        resp = client.post("/generate", data={"yaml": "name: T\ngame: Clique\n"},
                           headers={"Accept": "application/json"})
        assert resp.status_code == 410
        body = resp.get_json()
        assert body["retired"] is True
        # The old wizard prints `data.error` verbatim. If this stops being a sentence a player
        # can act on, the wizard shows them a shrug.
        assert "wizard" in body["error"].lower() and "archipelago.gg" in body["error"]

    def test_existing_rooms_are_untouched(self, client, room_id):
        """The whole reason creation was retired instead of the room routes being deleted."""
        assert client.get(f"/room/{room_id}").status_code == 200


# ---------------------------------------------------------------------------
# Test: start / stop
# ---------------------------------------------------------------------------

class TestStartStop:

    def test_start_room(self, client, room_id):
        resp = client.post(f"/room/{room_id}/start",
                           headers={"Accept": "application/json"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "RUNNING"

    def test_stop_room(self, client, room_id):
        # Start first
        client.post(f"/room/{room_id}/start", headers={"Accept": "application/json"})
        resp = client.post(f"/room/{room_id}/stop",
                           headers={"Accept": "application/json"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "HIBERNATED"

    def test_start_nonexistent(self, client):
        resp = client.post("/room/deadbeef/start",
                           headers={"Accept": "application/json"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test: health endpoint
# ---------------------------------------------------------------------------

class TestHealth:

    def test_health_returns_expected_fields(self, client, room_id):
        resp = client.get(f"/room/{room_id}/health")
        assert resp.status_code == 200
        h = resp.get_json()
        for field in ("id", "status", "port", "pid", "alive", "cpu_percent", "rss_mb"):
            assert field in h, f"health response missing field: {field}"

    def test_health_nonexistent(self, client):
        resp = client.get("/room/deadbeef/health")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test: logs endpoint
# ---------------------------------------------------------------------------

class TestLogs:

    def test_logs_returns_lines(self, client, room_id):
        resp = client.get(f"/room/{room_id}/logs")
        assert resp.status_code == 200
        body = resp.get_json()
        assert "lines" in body
        assert isinstance(body["lines"], list)

    def test_logs_nonexistent(self, client):
        resp = client.get("/room/deadbeef/logs")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test: delete
# ---------------------------------------------------------------------------

class TestDeleteRoom:

    def test_delete_existing(self, client, room_id):
        resp = client.delete(f"/room/{room_id}")
        assert resp.status_code == 200
        assert resp.get_json().get("ok") is True
        # Room should now be gone
        resp2 = client.get(f"/room/{room_id}", headers={"Accept": "application/json"})
        assert resp2.status_code == 404

    def test_delete_nonexistent(self, client):
        resp = client.delete("/room/deadbeef")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test: password
# ---------------------------------------------------------------------------

class TestPassword:

    def test_set_password(self, client, room_id):
        resp = client.post(f"/room/{room_id}/password",
                           data=json.dumps({"password": "secret"}),
                           content_type="application/json",
                           headers={"Accept": "application/json"})
        assert resp.status_code == 200
        assert resp.get_json()["password_set"] is True

    def test_clear_password(self, client, room_id):
        # Set then clear
        client.post(f"/room/{room_id}/password",
                    data=json.dumps({"password": "secret"}),
                    content_type="application/json",
                    headers={"Accept": "application/json"})
        resp = client.post(f"/room/{room_id}/password",
                           data=json.dumps({"password": None}),
                           content_type="application/json",
                           headers={"Accept": "application/json"})
        assert resp.status_code == 200
        assert resp.get_json()["password_set"] is False

    def test_password_nonexistent_room(self, client):
        resp = client.post("/room/deadbeef/password",
                           data=json.dumps({"password": "x"}),
                           content_type="application/json")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test: connect address correctness (the hard constraint from SPEC §1)
# ---------------------------------------------------------------------------

class TestConnectAddress:

    def test_wss_address_format(self, client, room_id):
        """The wss:// address returned must be well-formed: wss://host:PORT."""
        resp = client.get(f"/room/{room_id}", headers={"Accept": "application/json"})
        body = resp.get_json()
        wss = body["connect"]["wss"]
        if wss:
            assert wss.startswith("wss://"), f"wss address must start with wss://, got: {wss}"
            parts = wss[len("wss://"):].split(":")
            assert len(parts) == 2, f"wss must be host:port, got: {wss}"
            assert parts[1].isdigit(), f"port must be numeric, got: {parts[1]}"

    def test_web_url_and_connect_address_are_different(self, client, room_id):
        """
        SPEC §1 hard constraint: /room/<id> (web page) and wss://host:PORT (game)
        are different addresses and must not be conflated.
        """
        web_url = f"/room/{room_id}"
        resp = client.get(f"/room/{room_id}", headers={"Accept": "application/json"})
        body = resp.get_json()
        wss = body["connect"]["wss"] or ""
        # The web path should not appear in the wss address
        assert room_id not in wss, (
            f"wss address must NOT contain the room id ({room_id}); "
            f"room identity is the port, not the path. Got: {wss}"
        )

    def test_room_html_shows_wss_not_just_path(self, client, room_id):
        """The room page HTML must contain a wss:// address, not just the /room/<id> path."""
        resp = client.get(f"/room/{room_id}")
        html = resp.data.decode()
        assert "wss://" in html, "Room page must display the wss:// game connect address"

    def test_room_html_explains_port_identity(self, client, room_id):
        """Room page must communicate that the port IS the room identity."""
        resp = client.get(f"/room/{room_id}")
        html = resp.data.decode()
        # The page should mention port and the distinction (any phrasing)
        assert "port" in html.lower(), "Room page must mention 'port'"


# ---------------------------------------------------------------------------
# Test: donation link present everywhere
# ---------------------------------------------------------------------------

class TestDonationLink:
    """🛑 `/` DROPPED OUT OF THESE AT v0.4.0 AND THAT IS A REAL LOSS, NOT A TIDY-UP.

    The front page is now a static file served from ER_STATIC_DIR, built and gated in the
    er-archipelago repo. It does not pass through Jinja, so the context processor cannot reach
    it and no assertion made here can cover it -- this repo does not own that file's contents.

    So the coverage genuinely narrowed, and pretending otherwise by deleting the tests quietly
    would be the worse move. What is asserted here is what this app still renders. The landing
    page carries its own donation and contact links, and er-archipelago is where that is gated.
    """

    def test_donation_on_room_page(self, client, room_id):
        resp = client.get(f"/room/{room_id}")
        assert DONATION_URL in resp.data.decode()

    def test_donation_on_downloads(self, client):
        """The second templated page, so the context processor is still proven on more than one
        view -- which was the whole point of moving it off render_template kwargs."""
        html = client.get("/downloads").data.decode()
        assert DONATION_URL in html
        assert "coffee" in html.lower()


# ---------------------------------------------------------------------------
# Test: contact details on every page
# ---------------------------------------------------------------------------

class TestContactDetails:
    """The footer is chrome, so it must appear on EVERY page without the view
    remembering to pass it. These tests are the reason the values come from a context
    processor rather than a render_template kwarg: they hit two unrelated views."""

    @pytest.mark.parametrize("path", ["ROOM", "/downloads"])
    def test_contact_on_every_templated_page(self, client, room_id, path):
        """🛑 "/" WAS ONE OF THESE PARAMS AND IS NOT ANY MORE. It is a static file from another
        repo as of v0.4.0 (see TestFrontDoor), so it cannot be reached by a context processor
        and cannot be asserted from here. /downloads takes its place: two unrelated views is
        what makes this a test of the processor rather than of one template.
        """
        html = client.get(f"/room/{room_id}" if path == "ROOM" else path).data.decode()
        assert CONTACT_DISCORD in html, f"{path} is missing the Discord handle"
        assert CONTACT_GITHUB in html, f"{path} is missing the source link"

    def test_discord_handle_is_not_rendered_as_a_link(self, client, room_id):
        """A Discord handle has no profile URL. Rendering it as an <a href> would ship a
        dead link on every page, which is worse than plain text."""
        html = client.get(f"/room/{room_id}").data.decode()
        assert f'href="{CONTACT_DISCORD}"' not in html
        assert f"<code>{CONTACT_DISCORD}</code>" in html

    def test_donation_still_present_after_moving_to_context_processor(self, client, room_id):
        """Guards the refactor itself: donation_url stopped being a kwarg on three
        render_template calls, so prove the processor actually feeds them."""
        for html in (client.get("/downloads").data.decode(),
                     client.get(f"/room/{room_id}").data.decode()):
            assert DONATION_URL in html


# ---------------------------------------------------------------------------
# Test: the liveness probe opens no connection
# ---------------------------------------------------------------------------

class TestPortIsListening:
    """`/health` runs this on a 5 s timer per open room page. The previous
    connect-and-close implementation made `websockets` log
    `connection rejected (400 Bad Request)` on every single poll."""

    def test_true_for_a_listening_port(self):
        import socket as _s
        from webgui.orchestrator import port_is_listening
        srv = _s.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        try:
            assert port_is_listening(srv.getsockname()[1]) is True
        finally:
            srv.close()

    def test_false_for_a_closed_port(self):
        import socket as _s
        from webgui.orchestrator import port_is_listening
        srv = _s.socket(); srv.bind(("127.0.0.1", 0)); port = srv.getsockname()[1]
        srv.close()
        assert port_is_listening(port) is False

    def test_probe_accepts_no_connection(self):
        """THE POINT OF THE CHANGE. Probe a listening socket many times; the server must
        never see an accepted connection, because none was ever opened."""
        import socket as _s
        from webgui.orchestrator import port_is_listening
        if not os.path.exists("/proc/net/tcp"):
            pytest.skip("no /proc/net/tcp; probe falls back to connect() by design")
        srv = _s.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(8)
        srv.settimeout(0.2)
        try:
            port = srv.getsockname()[1]
            for _ in range(20):
                assert port_is_listening(port) is True
            with pytest.raises((BlockingIOError, OSError)):
                srv.accept()          # nothing queued == nothing connected
        finally:
            srv.close()


# ---------------------------------------------------------------------------
# Test: the deploy image installs what this app actually imports
# ---------------------------------------------------------------------------

HOST_REQS = os.path.join(REPO_DIR, "deploy", "docker", "requirements-host.txt")


class TestHostRequirementsCoverOurImports:
    """`deploy/docker/requirements-host.txt` is a HAND-CURATED subset of AP's root
    requirements, and the deploy image installs that file and nothing else. Anything this
    app imports has to be named in it, or the box runs without it."""

    @pytest.mark.parametrize("module", ["psutil", "flask"])
    def test_declared(self, module):
        if not os.path.exists(HOST_REQS):
            pytest.skip("deploy/docker/requirements-host.txt not present")
        with open(HOST_REQS) as fh:
            declared = [ln.split("#")[0].strip().lower() for ln in fh]
        assert any(d.startswith(module) for d in declared if d), (
            f"{module} is imported by the webgui but missing from requirements-host.txt"
        )


class TestHealthDegradesWithoutPsutil:
    """psutil is optional in the code and was missing from the deploy image, so the health
    card silently reported null CPU/RSS. Pin BOTH arms so the difference is visible rather
    than looking like 'the card is just like that'."""

    def test_null_when_psutil_is_unavailable(self):
        import importlib
        import webgui.orchestrator as orch
        real = sys.modules.get("psutil")
        sys.modules["psutil"] = None          # make `import psutil` raise ImportError
        try:
            importlib.reload(orch)
            assert orch.room_cpu_rss(os.getpid()) == (None, None)
            assert orch.resolve_server_pid(1234, 9, timeout=0.1) == 1234
        finally:
            if real is None:
                sys.modules.pop("psutil", None)
            else:
                sys.modules["psutil"] = real
            importlib.reload(orch)

    def test_numbers_when_psutil_is_available(self):
        pytest.importorskip("psutil")
        import webgui.orchestrator as orch
        cpu, rss = orch.room_cpu_rss(os.getpid())
        assert cpu is not None and rss is not None and rss > 0
