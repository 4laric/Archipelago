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
from webgui.app import create_app, DONATION_URL


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
def room_id(client):
    """Upload a valid room and return its id."""
    data = {
        "file": (io.BytesIO(_make_archipelago()), "test.archipelago"),
        "name": "Test Room",
    }
    resp = client.post("/rooms", data=data, content_type="multipart/form-data",
                       headers={"Accept": "application/json"})
    assert resp.status_code == 201, resp.data
    return resp.get_json()["id"]


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
# Test: POST /rooms (upload)
# ---------------------------------------------------------------------------

class TestCreateRoom:

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

class TestDashboard:

    def test_dashboard_loads(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "GoArchipelago" in html

    def test_dashboard_has_donation_link(self, client):
        resp = client.get("/")
        html = resp.data.decode()
        assert DONATION_URL in html, "Dashboard must contain the donation URL"
        assert "coffee" in html.lower()

    def test_dashboard_shows_rooms(self, client, room_id):
        resp = client.get("/")
        html = resp.data.decode()
        assert room_id in html, "Dashboard must show existing room IDs"

    def test_dashboard_empty_state(self, client):
        resp = client.get("/")
        html = resp.data.decode()
        assert "archipelago" in html.lower()


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

    def test_donation_on_dashboard(self, client):
        resp = client.get("/")
        assert DONATION_URL in resp.data.decode()

    def test_donation_on_room_page(self, client, room_id):
        resp = client.get(f"/room/{room_id}")
        assert DONATION_URL in resp.data.decode()

    def test_donation_link_text_mentions_coffee(self, client):
        resp = client.get("/")
        html = resp.data.decode()
        assert "coffee" in html.lower()
