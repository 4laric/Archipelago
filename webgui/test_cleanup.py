"""Retention distinguishes abandoned rooms from played rooms and preserves server logs."""

import io
import json
import zipfile
from pathlib import Path

from webgui import orchestrator as orch
from webgui.orchestrator import RoomManager, RoomStore


def _fake_archipelago() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("AP_test.archipelago", b"dummy-multidata")
    return buf.getvalue()


class _FakeProc:
    pid = 4321

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        pass

    def kill(self):
        pass


def _manager(tmp_path, **kwargs):
    return RoomManager(
        data_dir=str(tmp_path / "data"),
        store_path=str(tmp_path / "rooms.json"),
        repo_dir=str(tmp_path / "repo"),
        port_start=39100,
        port_end=39120,
        launcher=lambda cmd, **launch_kwargs: _FakeProc(),
        never_connected_retention=kwargs.pop("never_connected_retention", 100),
        used_room_retention=kwargs.pop("used_room_retention", 1000),
        **kwargs,
    )


def _room(manager):
    return manager.create_room(
        name="test",
        file_data=_fake_archipelago(),
        filename="test.archipelago",
    )


def test_never_connected_room_uses_short_retention(tmp_path):
    manager = _manager(tmp_path)
    room = _room(manager)

    assert manager.cleanup_stale_rooms(now=room.created_at + 99) == []
    assert manager.cleanup_stale_rooms(now=room.created_at + 100) == [room.id]
    assert manager.get_room(room.id) is None


def test_observed_connection_switches_room_to_long_retention(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    room = manager.start_room(_room(manager).id)
    monkeypatch.setattr(orch, "connections_on_port", lambda port: 1)
    connected_at = room.created_at + 10

    manager.hibernate_idle_rooms(now=connected_at)
    room = manager.get_room(room.id)
    assert room.first_connected_at == connected_at

    manager.stop_room(room.id)
    assert manager.cleanup_stale_rooms(now=connected_at + 999) == []
    assert manager.cleanup_stale_rooms(now=connected_at + 1000) == [room.id]


def test_running_room_is_never_retention_deleted(tmp_path):
    manager = _manager(tmp_path)
    room = manager.start_room(_room(manager).id)

    assert manager.cleanup_stale_rooms(now=room.created_at + 100_000) == []
    assert manager.get_room(room.id).status == "RUNNING"


def test_zero_retention_disables_cleanup(tmp_path):
    manager = _manager(tmp_path, never_connected_retention=0)
    room = _room(manager)

    assert manager.cleanup_stale_rooms(now=room.created_at + 100_000) == []
    assert manager.get_room(room.id) is not None


def test_server_log_is_archived_unchanged_before_room_deletion(tmp_path):
    manager = _manager(tmp_path)
    room = _room(manager)
    raw_log = b"[server] real output\nplayer said: hello\n"
    Path(room.log_path).write_bytes(raw_log)

    manager.cleanup_stale_rooms(now=room.created_at + 100)

    archives = list(Path(manager.log_archive_dir).rglob(f"{room.id}.server.log"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == raw_log
    assert not Path(room.multidata_path).parent.exists()


def test_archive_failure_keeps_room_and_source_log(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    room = _room(manager)
    Path(room.log_path).write_text("evidence", encoding="utf-8")

    def fail_copy(*args):
        raise OSError("disk full")

    monkeypatch.setattr(orch.shutil, "copy2", fail_copy)

    assert manager.cleanup_stale_rooms(now=room.created_at + 100) == []
    assert manager.get_room(room.id) is not None
    assert Path(room.log_path).read_text(encoding="utf-8") == "evidence"


def test_pre_feature_room_records_receive_the_safe_long_retention(tmp_path):
    path = tmp_path / "rooms.json"
    legacy = {
        "id": "legacy",
        "name": "legacy",
        "multidata_path": str(tmp_path / "legacy.archipelago"),
        "save_path": str(tmp_path / "legacy.apsave"),
        "created_at": 10.0,
        "last_active_at": 20.0,
    }
    path.write_text(json.dumps([legacy]), encoding="utf-8")

    room = RoomStore(str(path)).get("legacy")
    assert room.first_connected_at == 20.0
