"""
test_limits.py -- the three caps that keep one small box up, and the numbers behind each.

None of these are performance tuning. Each one is a way the box goes down that has already
happened, or that the sticky-port fix made reachable:

  1. SSE STREAMS. gunicorn runs ONE worker with 64 threads and every open log stream parks one for
     the life of the connection. When they are all parked the site stops answering with no crash,
     no log line and the container still "Up" -- gunicorn's --timeout only fires on workers that
     stop heartbeating, and a gthread worker heartbeats from its main loop while every request
     thread is blocked. This wedged peliarch.ca for three weeks, 2026-07-17 to 2026-08-07, at the
     then-value of 8 threads. Raising the thread count bought headroom and fixed nothing: a browser
     tab left open overnight holds its thread overnight.
  2. ROOM ADDRESS SPACE. Rooms are subprocesses of that same worker, so an OOM kill aimed at the
     worker takes every room with it. A per-room cap makes the room the thing that dies, and CRASHED
     already has a backoff-restart path.
  3. THE PUBLISHED PORT RANGE. It has to equal the range the allocator walks. A room outside it
     looks healthy from the inside -- listening, `alive: true` -- and is unreachable.

Run: pytest webgui/test_limits.py
"""
import io
import os
import re
import zipfile

import pytest

import webgui.app as app_module
from webgui.app import create_app
from webgui.orchestrator import (
    RoomManager, DEFAULT_PORT_START, DEFAULT_PORT_END, DEFAULT_ROOM_MAX_AS_MB, _room_limits,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DOCKER = os.path.join(os.path.dirname(HERE), "deploy", "docker")


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


# ---------------------------------------------------------------------------
# 1. Room address space
# ---------------------------------------------------------------------------

class TestRoomAddressSpaceCap:

    def _mgr(self, tmp_path, capture, **kw):
        def launcher(cmd, **kwargs):
            capture.update(kwargs)
            return _FakeProc()
        return RoomManager(
            data_dir=str(tmp_path / "d"), store_path=str(tmp_path / "rooms.json"),
            repo_dir=str(tmp_path / "r"), public_host="localhost",
            port_start=39200, port_end=39220, launcher=launcher, **kw
        )

    def _start(self, mgr):
        room = mgr.create_room(name="r", file_data=_fake_archipelago(),
                               filename="t.archipelago")
        mgr.start_room(room.id)

    @pytest.mark.skipif(os.name != "posix", reason="rlimits are posix-only")
    def test_a_room_is_launched_with_a_limit_it_cannot_raise(self, tmp_path):
        cap = {}
        self._start(self._mgr(tmp_path, cap))
        assert callable(cap.get("preexec_fn")), \
            "rooms must be spawned with the rlimit preexec, or one runaway room can OOM the worker "\
            "and take every other room down with it"

    def test_it_can_be_switched_off(self, tmp_path):
        """An operator on a box where this is wrong must be able to turn it off without a patch."""
        cap = {}
        self._start(self._mgr(tmp_path, cap, room_max_as_mb=0))
        assert cap.get("preexec_fn") is None

    @pytest.mark.skipif(os.name != "posix", reason="rlimits are posix-only")
    def test_the_limit_actually_applies_in_a_child(self):
        """Assert the SOFT AND HARD limit in a real fork, not just that a callable was passed.

        A preexec_fn that silently did nothing would satisfy the test above. This one runs it.
        """
        import resource
        pid = os.fork()
        if pid == 0:  # child
            code = 1
            try:
                _room_limits(64)()
                soft, hard = resource.getrlimit(resource.RLIMIT_AS)
                core_soft, _ = resource.getrlimit(resource.RLIMIT_CORE)
                code = 0 if (soft == hard == 64 * 1024 * 1024 and core_soft == 0) else 2
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        assert os.WEXITSTATUS(status) == 0, "RLIMIT_AS/RLIMIT_CORE were not applied in the child"

    def test_the_default_is_generous_against_the_measurement(self):
        """🛑 RLIMIT_AS caps ADDRESS SPACE, which CPython reserves far more of than it resides in.

        Measured on the live box 2026-08-13: a running room is ~160-200 MB RSS. A cap anywhere near
        that kills healthy rooms; this is a blast radius, not a diet.
        """
        assert DEFAULT_ROOM_MAX_AS_MB >= 8 * 200, \
            "the default cap is under 8x the measured working set -- that is a diet, not a blast "\
            "radius, and it will kill rooms that are behaving"


# ---------------------------------------------------------------------------
# 2. SSE streams
# ---------------------------------------------------------------------------

class TestSseCaps:

    @pytest.fixture
    def client_and_app(self, tmp_path, monkeypatch):
        from webgui.test_app import MockManager, _make_archipelago
        mgr = MockManager()
        room = mgr.create_room(name="r", file_data=_make_archipelago(),
                               filename="t.archipelago")
        app = create_app(manager=mgr)
        app.config["TESTING"] = True
        return app, app.test_client(), room.id

    def test_a_stream_ends_by_itself(self, client_and_app, monkeypatch):
        """The whole point: a tab nobody is watching must stop holding a thread.

        A 10 ms deadline asserts the loop is bounded at all, rather than measuring a duration -- a
        test that waited 900 s to prove a 900 s cap is a test nobody runs.

        🛑 NOT 0. Zero means NO CAP, which is the operator escape hatch, and using it here is how
        this test hung the suite on first run: `deadline = ... if SSE_MAX_SECONDS else None` read
        the 0 as "unlimited" and the generator never returned. The escape hatch is deliberate; the
        test just must not step on it.
        """
        app, client, room_id = client_and_app
        monkeypatch.setattr(app_module, "SSE_MAX_SECONDS", 0.01)
        body = client.get(f"/room/{room_id}/logs?stream=1").data.decode()
        assert "stream recycled" in body

    def test_the_slot_is_given_back_afterwards(self, client_and_app, monkeypatch):
        """A leaked slot is permanent, which is worse than the hang it was added to prevent."""
        app, client, room_id = client_and_app
        monkeypatch.setattr(app_module, "SSE_MAX_SECONDS", 0.01)
        for _ in range(3):
            assert client.get(f"/room/{room_id}/logs?stream=1").status_code == 200
        assert app.extensions["sse_streams"] == 0

    def test_past_the_ceiling_it_says_so_instead_of_hanging(self, client_and_app, monkeypatch):
        app, client, room_id = client_and_app
        monkeypatch.setattr(app_module, "SSE_MAX_STREAMS", 0)
        resp = client.get(f"/room/{room_id}/logs?stream=1")
        assert resp.status_code == 503
        assert "log viewers" in resp.get_json()["error"]

    def test_the_non_streaming_log_tail_is_never_refused(self, client_and_app, monkeypatch):
        """The fallback has to survive the ceiling, or hitting it means no logs at all."""
        app, client, room_id = client_and_app
        monkeypatch.setattr(app_module, "SSE_MAX_STREAMS", 0)
        assert client.get(f"/room/{room_id}/logs").status_code == 200

    def test_the_ceiling_leaves_threads_for_the_rest_of_the_site(self):
        """24 of 64. The failure was log viewers taking the LAST thread; a cap at or above the
        thread count would not have prevented it."""
        dockerfile = open(os.path.join(DOCKER, "Dockerfile"), encoding="utf-8").read()
        m = re.search(r"--threads\s+(\d+)", dockerfile)
        assert m, "gunicorn's --threads is gone from the Dockerfile; this gate now checks nothing"
        assert app_module.SSE_MAX_STREAMS < int(m.group(1)) / 2, \
            f"SSE_MAX_STREAMS={app_module.SSE_MAX_STREAMS} is not comfortably under the "\
            f"{m.group(1)} worker threads"


# ---------------------------------------------------------------------------
# 3. The published port range
# ---------------------------------------------------------------------------

class TestPublishedRangeMatchesTheAllocator:
    """A room on an unpublished port looks healthy from the inside and is unreachable.

    `port_is_listening` checks 127.0.0.1 INSIDE the container, so `alive: true` says nothing about
    whether a player can reach it. Nothing else in the system can notice this, which is why it is
    asserted here.
    """

    def _compose(self):
        return open(os.path.join(DOCKER, "docker-compose.yml"), encoding="utf-8").read()

    def test_the_compose_defaults_equal_the_allocator_defaults(self):
        m = re.search(r'\$\{PORT_START:-(\d+)\}-\$\{PORT_END:-(\d+)\}', self._compose())
        assert m, "the published port range is gone from docker-compose.yml"
        start, end = int(m.group(1)), int(m.group(2))
        assert start == DEFAULT_PORT_START
        # The allocator walks range(start, end) -- exclusive. Compose publishes inclusively.
        assert end == DEFAULT_PORT_END - 1, (
            f"compose publishes {start}-{end} while the allocator walks "
            f"{DEFAULT_PORT_START}..{DEFAULT_PORT_END - 1}. With PORT_END absent from .env a room "
            f"gets a port nobody outside the container can reach."
        )

    def test_the_env_example_agrees_with_both(self):
        env = open(os.path.join(DOCKER, ".env.example"), encoding="utf-8").read()
        start = int(re.search(r"^PORT_START=(\d+)", env, re.M).group(1))
        end = int(re.search(r"^PORT_END=(\d+)", env, re.M).group(1))
        assert (start, end) == (DEFAULT_PORT_START, DEFAULT_PORT_END - 1)
