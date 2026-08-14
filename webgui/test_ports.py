"""
test_ports.py -- one port per room, for the room's life, and no address unless it is live.

WHY THIS FILE EXISTS. Room hosting was scoped out at v0.4.0 over a single defect: the dashboard
offered five hibernated rooms the same connect address, ws://host:38400, each with a Copy button.
Archipelago's `Connect` packet carries a slot name and a password and NO room identifier, so a
client that reached whichever server actually held that port -- with a slot name that seed happened
to contain -- would join the wrong multiworld and be told nothing by either side.

The retirement note called the display half "an afternoon" and the failure mode "not an afternoon".
That was the right instinct about the wrong line: the display was reading a field that genuinely
lied, because the port was allocated per START and never released from the room record on STOP. So
these tests assert the two properties that make an address safe to publish, and they are properties
of the ORCHESTRATOR, not of a template:

  1. A room owns one port for its whole life, and no two rooms own the same one. A stale address
     therefore resolves to that room or to nothing -- never to somebody else's seed.
  2. An address exists only while the room is RUNNING. A port number is not an address.

🛑 11 OF THESE 12 FAIL AGAINST THE PRE-FIX ORCHESTRATOR -- checked by running them against
66062d9's webgui/orchestrator.py, not assumed, because a test that passes before the fix is not
evidence of the fix. The one that passed either way is
`test_restarting_a_room_gives_back_the_same_address`, and it passes for the wrong reason there: a
single room restarting alone happens to be handed the same free port back. It is kept as a
statement of the property, not as a discriminator.

Uses the injectable mock launcher, so no real servers are spawned.
Run: pytest webgui/test_ports.py
"""
import io
import json
import socket
import zipfile

import pytest

from webgui.orchestrator import Room, RoomManager


def _fake_archipelago() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("AP_test.archipelago", b"dummy-multidata")
    return buf.getvalue()


class _FakeProc:
    pid = 4321
    def poll(self): return None
    def terminate(self): pass
    def wait(self, timeout=None): pass
    def kill(self): pass


# The range is deliberately NOT 38400: these tests must not care which numbers they get, only that
# the numbers differ and stay put. A test that asserts 38400 would pass for the wrong reason.
PORT_START, PORT_END = 39000, 39020


def _manager(tmp_path, **kw):
    return RoomManager(
        data_dir=str(tmp_path / "data"),
        store_path=str(tmp_path / "rooms.json"),
        repo_dir=str(tmp_path / "repo"),
        public_host="testhost",
        port_start=kw.pop("port_start", PORT_START),
        port_end=kw.pop("port_end", PORT_END),
        launcher=lambda cmd, **kwargs: _FakeProc(),
        **kw
    )


def _room(mgr, name="room"):
    return mgr.create_room(name=name, file_data=_fake_archipelago(),
                           filename="test.archipelago")


# ---------------------------------------------------------------------------
# Property 1: one port per room, for the room's life
# ---------------------------------------------------------------------------

class TestOnePortPerRoom:

    def test_a_room_has_a_port_before_it_has_ever_started(self, tmp_path):
        """The port is part of the room, not of a launch."""
        room = _room(_manager(tmp_path))
        assert room.port is not None
        assert PORT_START <= room.port < PORT_END

    def test_two_rooms_never_share_a_port(self, tmp_path):
        mgr = _manager(tmp_path)
        a, b = _room(mgr, "a"), _room(mgr, "b")
        assert a.port != b.port

    def test_a_sleeping_room_still_owns_its_port(self, tmp_path):
        """THE MOTIVATING CASE. A stopped room's port is reserved, not free.

        Pre-fix, `start_room` called `free_port`, which asks the kernel and nothing else -- so the
        next room to wake took the sleeping room's number and both records claimed it.
        """
        mgr = _manager(tmp_path)
        a = _room(mgr, "a")
        mgr.start_room(a.id)
        held = a.port
        mgr.stop_room(a.id)
        assert mgr.get_room(a.id).port == held, "a hibernated room must keep its port"

        b = _room(mgr, "b")
        mgr.start_room(b.id)
        assert b.port != held, "the next room to start must not take a sleeping room's port"

    def test_restarting_a_room_gives_back_the_same_address(self, tmp_path):
        """The point of the reservation: an address you handed out last week still works."""
        mgr = _manager(tmp_path)
        a = _room(mgr, "a")
        first = mgr.start_room(a.id).port
        mgr.stop_room(a.id)
        assert mgr.start_room(a.id).port == first

    def test_running_out_of_ports_says_what_to_do(self, tmp_path):
        """Rule 2: a failure names the thing that was not done. Not an IndexError."""
        mgr = _manager(tmp_path, port_start=39100, port_end=39102)  # room for two
        _room(mgr, "a"), _room(mgr, "b")
        with pytest.raises(RuntimeError) as e:
            _room(mgr, "c")
        assert "PORT_START" in str(e.value)


# ---------------------------------------------------------------------------
# Property 2: an address exists only while the room is live
# ---------------------------------------------------------------------------

class TestAddressOnlyWhenLive:

    def test_a_sleeping_room_advertises_no_address(self, tmp_path):
        mgr = _manager(tmp_path)
        a = _room(mgr, "a")
        mgr.start_room(a.id)
        mgr.stop_room(a.id)
        ci = mgr.get_room(a.id).connect_info("testhost")
        assert ci["live"] is False
        assert ci["ws"] is None and ci["wss"] is None
        # The port is still reported -- an operator needs it. It is just not an address.
        assert ci["port"] is not None

    def test_a_never_started_room_advertises_no_address(self, tmp_path):
        ci = _room(_manager(tmp_path)).connect_info("testhost")
        assert ci["live"] is False and ci["ws"] is None

    def test_a_running_room_advertises_a_plain_ws_address(self, tmp_path):
        """ws://, not wss://. Room ports never touch Caddy, so they speak plaintext.

        A player who copies a wss:// room address gets SSLError: WRONG_VERSION_NUMBER.
        """
        mgr = _manager(tmp_path)
        a = _room(mgr, "a")
        mgr.start_room(a.id)
        ci = mgr.get_room(a.id).connect_info("testhost")
        assert ci["live"] is True
        assert ci["ws"] == f"ws://testhost:{a.port}"


# ---------------------------------------------------------------------------
# The migration: the five rooms already on the box all record 38400
# ---------------------------------------------------------------------------

class TestStoreIsDeDuplicatedOnLoad:

    def _store_with(self, tmp_path, ports):
        rooms = []
        for i, port in enumerate(ports):
            r = Room(id=f"room{i}", name=f"Player - Elden Ring",
                     multidata_path=f"/mock/{i}.archipelago", save_path=f"/mock/{i}.apsave",
                     port=port, status="HIBERNATED")
            rooms.append(r.to_dict())
        (tmp_path / "rooms.json").write_text(json.dumps(rooms), encoding="utf-8")

    def test_rooms_sharing_a_port_are_re_homed(self, tmp_path):
        """🛑 Sticky ports INHERIT the collision unless it is broken at load.

        Three rooms, all recording the same number, all named the same thing -- which is the live
        state on peliarch.ca, not a hypothetical.
        """
        self._store_with(tmp_path, [39000, 39000, 39000])
        mgr = _manager(tmp_path)
        ports = sorted(r.port for r in mgr.list_rooms())
        assert len(set(ports)) == 3, ports
        assert 39000 in ports, "the first claimant keeps its number; only the duplicates move"

    def test_a_room_with_no_port_gets_one(self, tmp_path):
        self._store_with(tmp_path, [None, None])
        mgr = _manager(tmp_path)
        ports = [r.port for r in mgr.list_rooms()]
        assert all(p is not None for p in ports) and len(set(ports)) == 2

    def test_a_port_outside_the_range_is_re_homed(self, tmp_path):
        """PORT_START/PORT_END can be narrowed by an operator; a room outside is unreachable."""
        self._store_with(tmp_path, [12345])
        mgr = _manager(tmp_path)
        assert PORT_START <= mgr.list_rooms()[0].port < PORT_END


# ---------------------------------------------------------------------------
# Refusing to start under someone else's port
# ---------------------------------------------------------------------------

class TestStartRefusesAnOccupiedPort:

    def test_it_does_not_quietly_move_the_room(self, tmp_path):
        """Quietly picking another port is how the original defect got shipped.

        If a room cannot have its own port, it does not start. An operator can read the error;
        a player cannot read a silent renumbering.
        """
        mgr = _manager(tmp_path)
        a = _room(mgr, "a")
        squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            squatter.bind(("0.0.0.0", a.port))
            squatter.listen(1)
            with pytest.raises(RuntimeError) as e:
                mgr.start_room(a.id)
            assert str(a.port) in str(e.value)
            assert mgr.get_room(a.id).port == a.port, "the room keeps its port; it just did not start"
        finally:
            squatter.close()

    def test_a_socket_cooling_down_from_this_room_is_not_a_squatter(self, tmp_path):
        """TIME_WAIT is not an occupied port, and refusing on it strands the room for a minute.

        THE MOTIVATING CASE (rule 11), 2026-08-13: a room was hibernated out from under a live
        game, the player pressed Start twice inside five seconds, and both attempts refused with
        "something else is holding it; check for an orphaned server process". The holder was the
        room's own listener, closed seconds earlier and still in TIME_WAIT. MultiServer would have
        bound it without complaint -- asyncio's `create_server` sets `reuse_address=True` on POSIX
        -- so the probe was answering a stricter question than the one being asked.

        The socket dance below is the deterministic way to produce that state: the SERVER side
        closes first, so TIME_WAIT lands here rather than on the client.
        """
        mgr = _manager(tmp_path)
        a = _room(mgr, "a")

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", a.port))
        listener.listen(1)
        client = socket.create_connection(("127.0.0.1", a.port))
        accepted, _ = listener.accept()
        accepted.close()          # server closes first -> TIME_WAIT on this side
        client.close()
        listener.close()

        room = mgr.start_room(a.id)
        assert room.status == "RUNNING", "a room refused to restart over its own cooling socket"
        assert room.port == a.port
