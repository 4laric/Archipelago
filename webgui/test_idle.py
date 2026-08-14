"""
test_idle.py -- a room sleeps when nobody is connected, and not one second sooner.

WHY THIS FILE EXISTS. `_idle_loop` hibernated a room when `now - room.last_active_at` passed its
`idle_timeout`. `last_active_at` had exactly two writers: `start_room`, and `touch_activity` --
which nothing in this codebase called. `app.py` only ever read the field, to display it; the only
other mention of the method was a stub in `test_app.py`'s fake manager. So the quantity being
compared against `idle_timeout` was not idleness. It was UPTIME.

THE MOTIVATING CASE (rule 11), 2026-08-13: a room was hosting a live game, the player had just
sent an item out, and thirty minutes after the room started it was SIGTERMed under him. Its log
shows `server closing` / `ConsoleTask cancelled` / `has left the game` -- an orderly shutdown, so
it was reported as a crash, which is what an orderly shutdown you did not ask for looks like from
the player's seat. Nothing he could have done would have prevented it: there was no code path that
could move `last_active_at` after the start.

4 OF THESE 7 FAIL AGAINST THE PRE-FIX REAPER -- checked by running them against it, not assumed.
The pre-fix loop had no seam to call, so the control was built by giving it exactly the seam
(`hibernate_idle_rooms`) and NOT the activity refresh: the fix minus its point. That is what makes
these tests evidence about the behaviour rather than about the extraction. The three that pass
either way are the properties that must SURVIVE the fix -- an empty room still sleeps, it sleeps
only once its timeout is up, and it keeps its port when it does.

Uses the injectable mock launcher, so no real servers are spawned. The connection count is faked
by monkeypatching `connections_on_port`, because a real ESTABLISHED socket on a room's port would
need a real server on it; `test_ports.py` is where real sockets are used, and it uses them to
assert the thing that genuinely needs a kernel.

Run: pytest webgui/test_idle.py
"""
import io
import zipfile

import pytest

from webgui import orchestrator as orch
from webgui.orchestrator import RoomManager


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


PORT_START, PORT_END = 39100, 39120


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


def _running_room(mgr, name="room", idle_timeout=1800):
    room = mgr.create_room(name=name, file_data=_fake_archipelago(),
                           filename="test.archipelago", idle_timeout=idle_timeout)
    return mgr.start_room(room.id)


@pytest.fixture
def connections(monkeypatch):
    """Fake the wire. `set(n)` is how many ESTABLISHED connections every room port carries."""
    state = {"n": 0}
    monkeypatch.setattr(orch, "connections_on_port", lambda port: state["n"])
    return lambda n: state.update(n=n)


class TestARoomWithPlayersStaysUp:

    def test_a_room_with_a_player_connected_is_not_hibernated(self, tmp_path, connections):
        """The whole defect, in one assertion.

        The room is well past its idle timeout by the clock -- which is all the pre-fix reaper
        looked at -- and somebody is connected to it.
        """
        mgr = _manager(tmp_path)
        room = _running_room(mgr, idle_timeout=60)
        connections(1)

        mgr.hibernate_idle_rooms(now=room.last_active_at + 10_000)

        assert mgr.get_room(room.id).status == "RUNNING", \
            "a room with a connection on its port was hibernated on a clock reading"

    def test_activity_is_refreshed_from_the_wire_not_from_the_web_page(self, tmp_path, connections):
        """`last_active_at` must actually MOVE, or the next pass sleeps the room anyway.

        Asserting only 'still RUNNING' would pass against an implementation that special-cases the
        decision without recording the activity -- and that implementation dies on the pass after.
        """
        mgr = _manager(tmp_path)
        room = _running_room(mgr, idle_timeout=60)
        started_at = mgr.get_room(room.id).last_active_at
        connections(2)

        later = started_at + 5_000
        mgr.hibernate_idle_rooms(now=later)
        assert mgr.get_room(room.id).last_active_at > started_at

        # the pass after: still nobody has touched a web page, still someone is playing
        mgr.hibernate_idle_rooms(now=later + 5_000)
        assert mgr.get_room(room.id).status == "RUNNING"


class TestAnEmptyRoomStillSleeps:

    def test_a_room_nobody_is_connected_to_hibernates(self, tmp_path, connections):
        mgr = _manager(tmp_path)
        room = _running_room(mgr, idle_timeout=60)
        connections(0)

        mgr.hibernate_idle_rooms(now=room.last_active_at + 61)

        assert mgr.get_room(room.id).status == "HIBERNATED"

    def test_an_empty_room_inside_its_timeout_is_left_alone(self, tmp_path, connections):
        mgr = _manager(tmp_path)
        room = _running_room(mgr, idle_timeout=60)
        connections(0)

        mgr.hibernate_idle_rooms(now=room.last_active_at + 59)

        assert mgr.get_room(room.id).status == "RUNNING"

    def test_a_hibernated_room_keeps_its_port(self, tmp_path, connections):
        """Sleeping is not releasing. The address a player already holds still resolves here."""
        mgr = _manager(tmp_path)
        room = _running_room(mgr, idle_timeout=60)
        held = room.port
        connections(0)

        mgr.hibernate_idle_rooms(now=room.last_active_at + 61)

        assert mgr.get_room(room.id).port == held


class TestIgnoranceKeepsTheRoomUp:

    def test_an_unreadable_table_is_not_read_as_empty(self, tmp_path, monkeypatch):
        """`connections_on_port` returns None when /proc/net/tcp will not read.

        None is not zero, and the difference is a live game. Reaping on 'I could not tell' costs
        somebody their session to save a few MB on a box that is not short of them.
        """
        mgr = _manager(tmp_path)
        room = _running_room(mgr, idle_timeout=60)
        monkeypatch.setattr(orch, "connections_on_port", lambda port: None)

        mgr.hibernate_idle_rooms(now=room.last_active_at + 10_000)

        assert mgr.get_room(room.id).status == "RUNNING"


class TestTheCounterReadsTheRightColumn:

    def test_it_counts_established_and_ignores_the_listener(self, tmp_path, monkeypatch):
        """0A is LISTEN, 01 is ESTABLISHED. Counting the listener means a room is never idle."""
        port = 39111
        hexport = f"{port:04X}"
        rows = [
            "  sl  local_address rem_address   st",
            f"   0: 00000000:{hexport} 00000000:0000 0A",                 # our listener
            f"   1: 0100007F:{hexport} 0200007F:C000 01",                 # a player
            f"   2: 0100007F:{hexport} 0300007F:C001 01",                 # another player
            f"   3: 0100007F:{hexport} 0400007F:C002 06",                 # TIME_WAIT
            "   4: 0100007F:1F90 0500007F:C003 01",                       # someone else's port
        ]
        import builtins
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            # tcp and tcp6 are two files and the counter reads both; feeding the same rows to
            # each would double every count and the test would be asserting its own fake.
            if str(path) == "/proc/net/tcp":
                return io.StringIO("\n".join(rows))
            if str(path) == "/proc/net/tcp6":
                raise OSError("no ipv6 table here")
            return real_open(path, *a, **kw)

        monkeypatch.setattr(builtins, "open", fake_open)
        assert orch.connections_on_port(port) == 2
