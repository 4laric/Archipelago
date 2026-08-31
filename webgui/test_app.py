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


@pytest.fixture
def running_room_id(mgr, room_id):
    """A room that is RUNNING, which is the only state that has a connect address.

    Needed because the interesting assertions about addresses are now BIDIRECTIONAL: the address
    has to be there when the room is up and absent when it is not, and a fixture that only ever
    produced one of those states is how the old tests came to pass off a label instead of a value.
    """
    mgr.start_room(room_id)
    return room_id


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

    def test_room_page_html_contains_connect_address(self, client, running_room_id):
        """A RUNNING room's page renders its actual address.

        🛑 THIS TEST USED TO ASSERT `"wss://" in html` ON A ROOM THAT WAS NOT RUNNING, and it
        passed -- off the words "wss:// (not enabled on room ports)" in a field LABEL. A substring
        that also occurs in the page's prose is not a witness for a value. Assert the address.
        """
        html = client.get(f"/room/{running_room_id}").data.decode()
        assert f"ws://{MockManager.DEFAULT_HOST}:38401" in html

    def test_room_page_html_contains_host_and_port(self, client, running_room_id):
        html = client.get(f"/room/{running_room_id}").data.decode()
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
# Test: the front door, the tab strip, and room creation
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

        Hosting IS advertised again -- as a tab (TestTabStrip) -- but the front door is the
        builder's page, not a room list. A visitor who has never generated a seed should not
        arrive at somebody else's multiworld.
        """
        monkeypatch.setattr(app_module, "ER_STATIC_DIR", str(tmp_path))
        (tmp_path / "landing.html").write_text("<h1>Elden Ring for Archipelago</h1>", encoding="utf-8")
        room = mgr.create_room(name="Secret Room", file_data=_make_archipelago(),
                               filename="test.archipelago")
        c = create_app(manager=mgr).test_client()
        html = c.get("/").data.decode()
        assert room.id not in html


class TestCreateRoom:
    """Room creation works again. Retired at v0.4.0, back at v0.4.1 with the defect fixed.

    🛑 THE 410 THAT USED TO BE ASSERTED HERE IS GONE, BUT ITS AUDIENCE IS NOT: an options wizard
    already open in somebody's browser, and the file:// wizard inside every previous release zip,
    both POST here and both render `data.error` verbatim. That is why every rejection below is a
    sentence and not a bare code -- see the 400s.

    The two properties that made it safe to offer this again are in webgui/test_ports.py, because
    they are properties of the allocator, not of this route: one port per room for the room's life,
    and no address published unless the room is RUNNING.
    """


    def test_upload_valid_returns_201_json(self, client):
        data = {
            "file": (io.BytesIO(_make_archipelago()), "myroom.archipelago"),
            "name": "My Room",
        }
        resp = client.post("/rooms", data=data, content_type="multipart/form-data",
                           headers={"Accept": "application/json"})
        assert resp.status_code == 201
        body = resp.get_json()
        assert "id" in body
        assert body["name"] == "My Room"
        assert body["status"] in ("UPLOADED", "RUNNING", "STARTING")

    def test_upload_returns_connect_info(self, client):
        data = {
            "file": (io.BytesIO(_make_archipelago()), "myroom.archipelago"),
            "name": "Connect Test",
        }
        resp = client.post("/rooms", data=data, content_type="multipart/form-data",
                           headers={"Accept": "application/json"})
        assert resp.status_code == 201
        body = resp.get_json()
        assert "connect" in body
        ci = body["connect"]
        assert "host" in ci and "port" in ci and "wss" in ci
        # wss address must contain the host and port
        if ci["wss"]:
            assert ci["host"] in ci["wss"]
            assert str(ci["port"]) in ci["wss"]

    def test_upload_wrong_extension_rejected(self, client):
        data = {
            "file": (io.BytesIO(b"dummy"), "not_a_room.txt"),
            "name": "Bad File",
        }
        resp = client.post("/rooms", data=data, content_type="multipart/form-data",
                           headers={"Accept": "application/json"})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_upload_size_cap_rejected(self, client, mgr):
        # Set a tiny cap on the mock manager
        mgr.upload_max_bytes = 100
        # Build a zip that will pass the Flask content-length check but fail validation
        data = {
            "file": (io.BytesIO(_make_archipelago(200)), "big.archipelago"),
            "name": "Oversized",
        }
        resp = client.post("/rooms", data=data, content_type="multipart/form-data",
                           headers={"Accept": "application/json"})
        # Should be 400 (validation error from orchestrator) or 413 (Flask MAX_CONTENT_LENGTH)
        assert resp.status_code in (400, 413)

    def test_upload_no_file_rejected(self, client):
        resp = client.post("/rooms", data={"name": "no file"},
                           content_type="multipart/form-data",
                           headers={"Accept": "application/json"})
        assert resp.status_code == 400

    def test_html_upload_redirects_to_room(self, client):
        """Non-JSON client gets redirected to /room/<id>."""
        data = {
            "file": (io.BytesIO(_make_archipelago()), "myroom.archipelago"),
            "name": "Redirect Test",
        }
        resp = client.post("/rooms", data=data, content_type="multipart/form-data",
                           follow_redirects=False)
        assert resp.status_code == 302
        assert "/room/" in resp.headers.get("Location", "")

    def test_existing_rooms_are_untouched(self, client, room_id):
        """Kept from the retirement suite: rooms that predate all of this still serve."""
        assert client.get(f"/room/{room_id}").status_code == 200


class TestTabStrip:
    """One strip, six destinations, same on every templated page -- and the BUILDER IS FIRST.

    🛑 THIS IS HALF OF THE DEFINITION. The other half is er-archipelago's `wizard/tabs.js`, which
    renders the same six links into landing.html, wizard.html, checks.html and report.html --
    single files that never pass through Jinja and so cannot inherit base.html. Two copies is a
    deliberate trade (a templated page whose navigation came from a 404ing script would have no
    navigation at all), and these assertions are the price of it: change a tab here and this test
    tells you, in this repo, that the other copy needs the same change.
    """

    TABS = [
        "/er/", "/downloads", "/hosting", "/er/questlines.html", "/er/checks.html",
        "/er/report.html",
    ]

    def _html(self, mgr, tmp_path, monkeypatch, path="/hosting"):
        (tmp_path / "questlines.html").write_text("questline DAG", encoding="utf-8")
        monkeypatch.setattr(app_module, "ER_STATIC_DIR", str(tmp_path))
        c = create_app(manager=mgr).test_client()
        return c.get(path).data.decode()

    def test_every_tab_is_present_and_in_order(self, mgr, tmp_path, monkeypatch):
        html = self._html(mgr, tmp_path, monkeypatch)
        found = [t for t in self.TABS if f'href="{t}"' in html]
        assert found == self.TABS, f"missing or reordered: {found}"

    def test_the_builder_comes_before_hosting(self, mgr, tmp_path, monkeypatch):
        """The site is a yaml builder that can also host a room, not the other way round."""
        html = self._html(mgr, tmp_path, monkeypatch)
        assert html.index('href="/er/"') < html.index('href="/hosting"')

    def test_the_current_tab_is_marked(self, mgr, tmp_path, monkeypatch):
        html = self._html(mgr, tmp_path, monkeypatch)
        assert '<a href="/hosting" class="on">' in html

    def test_the_room_page_counts_as_hosting(self, mgr, tmp_path, monkeypatch):
        room = mgr.create_room(name="R", file_data=_make_archipelago(),
                               filename="t.archipelago")
        html = self._html(mgr, tmp_path, monkeypatch, path=f"/room/{room.id}")
        assert '<a href="/hosting" class="on">' in html

    def test_tabs_that_need_er_tooling_vanish_without_it(self, mgr, monkeypatch):
        """A tab that 404s is worse than a tab that is not there.

        Builder, Questlines, Checks and Report all serve out of ER_STATIC_DIR. Downloads and Hosting are
        peliarch's own routes and must survive.
        """
        monkeypatch.setattr(app_module, "ER_STATIC_DIR", "")
        html = create_app(manager=mgr).test_client().get("/hosting").data.decode()
        assert "/er/" not in html
        assert "/er/questlines.html" not in html
        assert 'href="/downloads"' in html and 'href="/hosting"' in html

    def test_hosting_support_links_to_er_report_when_tooling_exists(
            self, mgr, tmp_path, monkeypatch):
        html = self._html(mgr, tmp_path, monkeypatch)
        assert 'data-testid="hosting-support"' in html
        assert 'href="/er/report.html"' in html

    def test_questlines_tab_vanishes_when_artifact_is_absent(self, mgr, tmp_path, monkeypatch):
        """An older ER_REF may have tooling but no optional questline DAG."""
        monkeypatch.setattr(app_module, "ER_STATIC_DIR", str(tmp_path))
        html = create_app(manager=mgr).test_client().get("/hosting").data.decode()
        assert 'href="/er/"' in html
        assert 'href="/er/questlines.html"' not in html


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
    """The address is published when it is real and withheld when it is not.

    THE MOTIVATING CASE (rule 11), 2026-08-12: five hibernated rooms each showed
    ws://host:38400 with a Copy button, and nothing was listening on any of them. Archipelago's
    Connect packet carries a slot name and a password and NO room identifier, so a client that
    reached whichever server actually held that port -- with a slot name that seed happened to
    contain -- joined the wrong multiworld silently. Hosting was scoped out over it.

    🛑 THE TESTS THAT USED TO LIVE HERE COULD NOT HAVE CAUGHT IT. One was
    `assert "wss://" in html`, satisfied by a field label. One was `if wss:` around its
    assertions, so it asserted nothing at all once wss became None. Both were green the whole
    time the bug was live. Every assertion below names a state and a value.
    """

    def test_a_running_room_publishes_a_plain_ws_address(self, client, running_room_id):
        body = client.get(f"/room/{running_room_id}",
                          headers={"Accept": "application/json"}).get_json()
        ci = body["connect"]
        assert ci["live"] is True
        assert ci["ws"] == f"ws://{MockManager.DEFAULT_HOST}:{ci['port']}"

    def test_a_room_that_is_not_running_publishes_nothing(self, client, room_id):
        """Not "" and not the port -- None. A caller must not be able to build an address."""
        body = client.get(f"/room/{room_id}",
                          headers={"Accept": "application/json"}).get_json()
        ci = body["connect"]
        assert ci["live"] is False
        assert ci["ws"] is None and ci["wss"] is None
        assert ci["port"] is not None, "the reserved port is still reported; it is not an address"

    def test_a_hibernated_room_page_offers_no_copyable_address(self, client, running_room_id, mgr):
        """The Copy button is the promise, so this asserts on the ADDRESS STRING, not the button.

        Start it, stop it, and the address that was on the page has to be gone from the page.
        """
        host = MockManager.DEFAULT_HOST
        assert f"ws://{host}:38401" in client.get(f"/room/{running_room_id}").data.decode()
        mgr.stop_room(running_room_id)
        html = client.get(f"/room/{running_room_id}").data.decode()
        assert f"ws://{host}:38401" not in html
        assert "Nothing is listening yet" in html

    def test_the_hosting_table_does_not_offer_a_sleeping_room_an_address(self, mgr, tmp_path,
                                                                        monkeypatch):
        """The exact surface that shipped the defect: the room list, with its Copy buttons."""
        monkeypatch.setattr(app_module, "ER_STATIC_DIR", str(tmp_path))
        c = create_app(manager=mgr).test_client()
        room = mgr.create_room(name="Player - Elden Ring", file_data=_make_archipelago(),
                               filename="t.archipelago")
        mgr.start_room(room.id)
        mgr.stop_room(room.id)
        html = c.get("/hosting").data.decode()
        assert f"ws://{MockManager.DEFAULT_HOST}:38401" not in html
        assert "copyText('ws://" not in html, "a Copy button is a promise that it works"
        assert "reserved port 38401" in html

    def test_the_page_no_longer_says_connecting_wakes_a_sleeping_room(self, client,
                                                                     running_room_id, mgr):
        """It never did. stop_room kills the process, so a connect to a sleeping room is refused.

        Wake-on-connect needs a listener on the room's port; the room owning that port for life is
        its prerequisite and is now true, so it is buildable. Until it is built, nothing claims it.
        """
        mgr.stop_room(running_room_id)
        html = client.get(f"/room/{running_room_id}").data.decode()
        assert "connect to wake" not in html
        assert "wakes automatically" not in html
        assert "Start" in html

    def test_web_url_and_connect_address_are_different(self, client, running_room_id):
        """
        SPEC 1 hard constraint: /room/<id> (web page) and ws://host:PORT (game)
        are different addresses and must not be conflated.
        """
        body = client.get(f"/room/{running_room_id}",
                          headers={"Accept": "application/json"}).get_json()
        ws = body["connect"]["ws"]
        assert running_room_id not in ws, (
            f"connect address must NOT contain the room id ({running_room_id}); "
            f"room identity is the port, not the path. Got: {ws}"
        )

    def test_room_html_explains_port_identity(self, client, room_id):
        """Room page must communicate that the port IS the room identity."""
        html = client.get(f"/room/{room_id}").data.decode()
        assert "port" in html.lower(), "Room page must mention 'port'"
        assert "belongs to" in html, "and that this room's port is its own"


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

    def test_hosting_page_promotes_room_support_before_upload(self, client):
        html = client.get("/hosting").data.decode()
        card = html.index('data-testid="hosting-support"')
        upload = html.index('id="upload-form"')
        assert card < upload
        assert "Need help with a room?" in html
        assert f"copyText('{CONTACT_DISCORD}', this)" in html
        assert f"<code>{CONTACT_DISCORD}</code>" in html


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
