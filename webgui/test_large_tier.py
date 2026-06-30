"""
test_large_tier.py — verifies the orchestrator launches the right backend per tier:
  Standard → stock MultiServer.py
  Large    → peliarch (Go), with --multidata bundle + --password
Uses the injectable mock launcher, so no real processes are spawned.

Run: pytest webgui/test_large_tier.py
"""
import io
import zipfile

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


def _manager(tmp_path, capture):
    def launcher(cmd, **kwargs):
        capture["cmd"] = cmd
        return _FakeProc()
    return RoomManager(
        data_dir=str(tmp_path / "data"),
        store_path=str(tmp_path / "rooms.json"),
        repo_dir=str(tmp_path / "repo"),
        public_host="localhost",
        port_start=39000, port_end=39100,
        launcher=launcher,
    )


def test_large_tier_launches_peliarch(tmp_path):
    cap = {}
    mgr = _manager(tmp_path, cap)
    room = mgr.create_room(
        name="big", file_data=_fake_archipelago(), filename="big.archipelago",
        tier="Large", password="secret",
    )
    mgr.start_room(room.id)
    cmd = [str(c) for c in cap["cmd"]]
    assert any("peliarch" in c for c in cmd), cmd
    assert "--multidata" in cmd, cmd
    assert "--password" in cmd and "secret" in cmd, cmd
    assert not any("MultiServer.py" in c for c in cmd), cmd


def test_standard_tier_launches_multiserver(tmp_path):
    cap = {}
    mgr = _manager(tmp_path, cap)
    room = mgr.create_room(
        name="small", file_data=_fake_archipelago(), filename="small.archipelago",
    )  # tier defaults to Standard
    mgr.start_room(room.id)
    cmd = [str(c) for c in cap["cmd"]]
    assert any("MultiServer.py" in c for c in cmd), cmd
    assert not any("peliarch" in c for c in cmd), cmd
