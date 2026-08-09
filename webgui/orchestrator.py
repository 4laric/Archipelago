"""
orchestrator.py — RoomManager for Peliarch web GUI.

Manages the lifecycle of per-room MultiServer processes:
  UPLOADED → STARTING → RUNNING → HIBERNATED → (re-STARTING)
                                              → CRASHED → (restart)

Process supervision helpers are re-implemented here (free_port, wait_for_port,
resolve_server_pid, stop_server) so this module has no import dependency on
run_loadtest.py, which has a __main__ guard but still pollutes the namespace.
The logic is identical to run_loadtest.py's helpers.

The orchestrator is injectable/mockable: pass launcher=... to RoomManager to
replace the actual subprocess.Popen call — used by tests to avoid spawning
real servers.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants (override via environment or pass to RoomManager)
# ---------------------------------------------------------------------------

DEFAULT_PORT_START = 38400
DEFAULT_PORT_END   = 38600
DEFAULT_IDLE_TIMEOUT = 30 * 60      # 30 minutes
DEFAULT_UPLOAD_MAX_BYTES = 64 * 1024 * 1024   # 64 MB
MAX_CRASH_RESTARTS = 5
CRASH_BACKOFF_BASE = 5.0            # seconds; doubles each restart

# ---------------------------------------------------------------------------
# Process helpers (mirrors run_loadtest.py; no circular import)
# ---------------------------------------------------------------------------

def free_port(preferred: int, port_end: int = DEFAULT_PORT_END) -> int:
    """Return preferred if free, else scan upward for a free port."""
    for p in range(preferred, port_end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"No free port found between {preferred} and {port_end}")


def _connect_probe(host: str, port: int) -> bool:
    """Fallback liveness check: open a TCP connection and drop it.

    🛑 THIS IS THE NOISY ONE. It is only reached where the kernel socket table is not
    readable (macOS/Windows dev boxes). See `port_is_listening`.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def port_is_listening(port: int, host: str = "127.0.0.1") -> bool:
    """True if something is LISTENing on `port`, WITHOUT opening a connection.

    🛑🛑 WHY NOT connect_ex. Connecting and immediately closing is the obvious
    implementation and it is what filled every room log with

        connection rejected (400 Bad Request)
        connection closed

    once every 5 seconds, forever: `room.html` polls /health on a timer, each poll
    connected to the room port and closed without sending a byte, and `websockets`
    reads for an HTTP request line, hits EOF and rejects the "request". Measured: a
    bare connect-and-close and a real TLS ClientHello produce the SAME 400, so the log
    line could not even be told apart from a player failing to connect -- the noise was
    actively misleading, not merely ugly.

    Reading /proc/net/tcp{,6} answers the same question and touches nothing. Column 1
    is `HEXIP:HEXPORT`, column 3 is the state, and 0A is TCP_LISTEN. The address family
    and bind address are deliberately ignored: "is this port served" is the question,
    exactly as it was for the connect probe.

    Rooms run as child processes of this app, so they share its network namespace and
    show up in the same table.
    """
    target = f"{port:04X}"
    readable = False
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as fh:
                rows = fh.read().splitlines()[1:]
        except OSError:
            continue
        readable = True
        for row in rows:
            cols = row.split()
            if len(cols) > 3 and cols[3] == "0A" and cols[1].rsplit(":", 1)[-1].upper() == target:
                return True
    if readable:
        return False
    return _connect_probe(host, port)


def wait_for_port(host: str, port: int, timeout: float = 60.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if port_is_listening(port, host):
            return True
        time.sleep(0.5)
    return False


def resolve_server_pid(launch_pid: int, port: int, timeout: float = 10.0) -> int:
    """Find the PID actually LISTENING on port (handles ModuleUpdate re-exec on Windows)."""
    try:
        import psutil
    except ImportError:
        return launch_pid

    end = time.time() + timeout
    while time.time() < end:
        try:
            for c in psutil.net_connections(kind="inet"):
                if (c.laddr and c.laddr.port == port and c.pid
                        and c.status == psutil.CONN_LISTEN):
                    return c.pid
        except Exception:
            pass
        try:
            kids = psutil.Process(launch_pid).children(recursive=True)
            if kids:
                def _rss(p):
                    try:
                        return p.memory_info().rss
                    except Exception:
                        return 0
                return max(kids, key=_rss).pid
        except Exception:
            pass
        time.sleep(0.5)
    return launch_pid


def stop_server(proc) -> None:
    """Cross-platform graceful stop (SIGTERM / TerminateProcess), then SIGKILL."""
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass
    time.sleep(0.3)


def room_cpu_rss(server_pid: Optional[int]):
    """Return (cpu_percent, rss_mb) for the given PID, or (None, None)."""
    if server_pid is None:
        return None, None
    try:
        import psutil
        p = psutil.Process(server_pid)
        p.cpu_percent(None)   # prime
        time.sleep(0.1)
        cpu = p.cpu_percent(None)
        rss = p.memory_info().rss / (1024 * 1024)
        return round(cpu, 1), round(rss, 1)
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------------

UPLOAD_MAX_BYTES = DEFAULT_UPLOAD_MAX_BYTES


def validate_archipelago_upload(data: bytes, max_bytes: int = UPLOAD_MAX_BYTES) -> None:
    """
    Validate a raw upload.  Raises ValueError with a human-readable message on failure.
    - Size cap
    - Must be a valid ZIP/archipelago file
    - Must contain at least one entry that looks like multidata
    - Never uses plain pickle.load; restricted_loads is used at serve time by MultiServer
    """
    if len(data) > max_bytes:
        raise ValueError(
            f"Upload too large: {len(data)//1024} KB exceeds cap of {max_bytes//1024} KB"
        )
    if len(data) < 4:
        raise ValueError("Upload is too small to be a valid .archipelago file")

    # .archipelago files ARE zips
    try:
        import io
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = zf.namelist()
        zf.close()
    except zipfile.BadZipFile:
        raise ValueError("Upload is not a valid ZIP/.archipelago file")

    if not names:
        raise ValueError("Upload ZIP is empty")

    # Sanity: at least one entry should look like multidata (no path traversal)
    for name in names:
        if ".." in name or name.startswith("/"):
            raise ValueError(f"ZIP contains unsafe path: {name!r}")

    # Do NOT pickle.load here.  MultiServer uses restricted_loads (Utils.py).
    # We only validate structure.


# ---------------------------------------------------------------------------
# Room record
# ---------------------------------------------------------------------------

ROOM_STATES = {"UPLOADED", "STARTING", "RUNNING", "HIBERNATED", "CRASHED", "STOPPED"}


@dataclass
class Room:
    id: str
    name: str
    multidata_path: str        # absolute path to saved .archipelago file
    save_path: str             # absolute path to .apsave (may not exist yet)
    port: Optional[int] = None
    status: str = "UPLOADED"
    password: Optional[str] = None
    tier: str = "Standard"
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    pid: Optional[int] = None          # PID of process listening on port
    launch_pid: Optional[int] = None   # Popen.pid (may differ on Windows)
    crash_count: int = 0
    log_path: Optional[str] = None
    # not persisted — only in-memory
    _proc: object = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        # NB: do NOT use asdict() here — it deep-copies every field, including the
        # live subprocess.Popen in _proc (which holds a thread lock and can't be
        # copied/pickled). Build the persistable dict from the scalar fields directly.
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "_proc"}

    @staticmethod
    def from_dict(d: dict) -> "Room":
        d = dict(d)
        d.pop("_proc", None)
        return Room(**d)

    def connect_info(self, public_host: str) -> dict:
        return {
            "host": public_host,
            "port": self.port,
            "wss": f"wss://{public_host}:{self.port}" if self.port else None,
            "ws":  f"ws://{public_host}:{self.port}"  if self.port else None,
        }


# ---------------------------------------------------------------------------
# Persistence (JSON file store)
# ---------------------------------------------------------------------------

class RoomStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._rooms: Dict[str, Room] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    data = json.load(f)
                for d in data:
                    r = Room.from_dict(d)
                    self._rooms[r.id] = r
                logger.info("Loaded %d rooms from %s", len(self._rooms), self.path)
            except Exception as e:
                logger.warning("Could not load room store %s: %s", self.path, e)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump([r.to_dict() for r in self._rooms.values()], f, indent=2)
        os.replace(tmp, self.path)

    def all(self) -> List[Room]:
        with self._lock:
            return list(self._rooms.values())

    def get(self, room_id: str) -> Optional[Room]:
        with self._lock:
            return self._rooms.get(room_id)

    def put(self, room: Room):
        with self._lock:
            self._rooms[room.id] = room
            self._save()

    def delete(self, room_id: str):
        with self._lock:
            self._rooms.pop(room_id, None)
            self._save()


# ---------------------------------------------------------------------------
# RoomManager
# ---------------------------------------------------------------------------

class RoomManager:
    """
    Manages the full lifecycle of AP rooms.

    Parameters
    ----------
    data_dir : str
        Root directory for room data (multidata, saves, logs).
    store_path : str
        Path to the JSON room-record file.
    repo_dir : str
        Path to the Archipelago repo root (where MultiServer.py lives).
    public_host : str
        Hostname/IP players use to connect (shown in the connect block).
    port_start / port_end : int
        Port allocation range.
    launcher : callable or None
        Override for subprocess.Popen (used by tests to avoid real processes).
        Signature: launcher(cmd, **kwargs) -> proc-like object with .pid, .poll(),
        .terminate(), .wait(), .kill() and optionally .stdout.
    """

    def __init__(
        self,
        data_dir: str,
        store_path: str,
        repo_dir: str,
        public_host: str = "localhost",
        port_start: int = DEFAULT_PORT_START,
        port_end: int = DEFAULT_PORT_END,
        launcher: Optional[Callable] = None,
        upload_max_bytes: int = DEFAULT_UPLOAD_MAX_BYTES,
    ):
        self.data_dir = data_dir
        self.repo_dir = repo_dir
        self.public_host = public_host
        self.port_start = port_start
        self.port_end = port_end
        self.upload_max_bytes = upload_max_bytes
        self._launcher = launcher  # None → use real subprocess.Popen
        self._store = RoomStore(store_path)
        self._lock = threading.Lock()
        self._idle_thread: Optional[threading.Thread] = None
        self._stop_idle = threading.Event()

        os.makedirs(data_dir, exist_ok=True)

        # Resurrect any rooms that were RUNNING at last shutdown → HIBERNATED
        for room in self._store.all():
            if room.status in ("RUNNING", "STARTING"):
                room.status = "HIBERNATED"
                room._proc = None
                room.pid = None
                room.launch_pid = None
                self._store.put(room)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_room(
        self,
        name: str,
        file_data: bytes,
        filename: str,
        password: Optional[str] = None,
        idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
        tier: str = "Standard",
    ) -> Room:
        """Validate upload, persist multidata, create room record."""
        validate_archipelago_upload(file_data, self.upload_max_bytes)

        room_id = str(uuid.uuid4())[:8]
        room_dir = os.path.join(self.data_dir, room_id)
        os.makedirs(room_dir, exist_ok=True)

        # Sanitize filename
        safe_name = Path(filename).name
        if not safe_name.endswith((".archipelago", ".zip")):
            safe_name = safe_name + ".archipelago"
        multidata_path = os.path.join(room_dir, safe_name)

        with open(multidata_path, "wb") as f:
            f.write(file_data)

        save_path  = os.path.join(room_dir, f"{room_id}.apsave")
        log_path   = os.path.join(room_dir, "server.log")

        room = Room(
            id=room_id,
            name=name,
            multidata_path=multidata_path,
            save_path=save_path,
            password=password,
            idle_timeout=idle_timeout,
            tier=tier,
            log_path=log_path,
        )
        self._store.put(room)
        logger.info("Created room %s (%s)", room_id, name)
        return room

    def list_rooms(self) -> List[Room]:
        return self._store.all()

    def get_room(self, room_id: str) -> Optional[Room]:
        return self._store.get(room_id)

    def start_room(self, room_id: str) -> Room:
        """Transition room to RUNNING (allocate port, launch MultiServer)."""
        room = self._store.get(room_id)
        if room is None:
            raise KeyError(f"Room {room_id} not found")
        if room.status == "RUNNING":
            return room

        with self._lock:
            room.status = "STARTING"
            self._store.put(room)

        try:
            port = free_port(self.port_start, self.port_end)
            room.port = port
            # Tier selects the backend: Large rooms run the Go server (peliarch),
            # everything else runs stock MultiServer.
            if room.tier == "Large":
                proc = self._launch_peliarch(room)
            else:
                proc = self._launch_multiserver(room)
            room._proc = proc
            room.launch_pid = proc.pid

            if self._launcher is None:
                # Real launch: wait for port to open
                ok = wait_for_port("127.0.0.1", port, timeout=60)
                if not ok:
                    stop_server(proc)
                    room.status = "CRASHED"
                    room.crash_count += 1
                    self._store.put(room)
                    raise RuntimeError(f"MultiServer never opened port {port}")
                room.pid = resolve_server_pid(proc.pid, port)
            else:
                # Mock launch (tests): skip port wait
                room.pid = proc.pid

            room.status = "RUNNING"
            room.last_active_at = time.time()
            self._store.put(room)
            logger.info("Room %s RUNNING on port %d (pid=%s)", room_id, port, room.pid)
        except Exception as e:
            room.status = "CRASHED"
            self._store.put(room)
            raise

        return room

    def stop_room(self, room_id: str) -> Room:
        room = self._store.get(room_id)
        if room is None:
            raise KeyError(f"Room {room_id} not found")
        if room.status not in ("RUNNING", "STARTING"):
            return room
        stop_server(room._proc)
        room._proc = None
        room.pid = None
        room.launch_pid = None
        room.status = "HIBERNATED"
        self._store.put(room)
        logger.info("Room %s stopped → HIBERNATED", room_id)
        return room

    def delete_room(self, room_id: str) -> None:
        room = self._store.get(room_id)
        if room is None:
            return
        if room.status == "RUNNING":
            self.stop_room(room_id)
        # Remove files
        room_dir = os.path.dirname(room.multidata_path)
        try:
            import shutil
            shutil.rmtree(room_dir, ignore_errors=True)
        except Exception as e:
            logger.warning("Could not remove room dir %s: %s", room_dir, e)
        self._store.delete(room_id)
        logger.info("Room %s deleted", room_id)

    def set_password(self, room_id: str, password: Optional[str]) -> Room:
        room = self._store.get(room_id)
        if room is None:
            raise KeyError(f"Room {room_id} not found")
        room.password = password
        self._store.put(room)
        # If running, a restart is needed for the password to take effect.
        # We mark it here; the caller can restart if desired.
        return room

    def health(self, room_id: str) -> dict:
        room = self._store.get(room_id)
        if room is None:
            return {"error": "not found"}
        cpu, rss = room_cpu_rss(room.pid)
        alive = False
        if room.status == "RUNNING" and room.port:
            # NOT a connect probe -- this runs on a 5 s timer per open room page.
            alive = port_is_listening(room.port)
        return {
            "id": room_id,
            "status": room.status,
            "port": room.port,
            "pid": room.pid,
            "alive": alive,
            "cpu_percent": cpu,
            "rss_mb": rss,
        }

    def log_tail(self, room_id: str, lines: int = 100) -> List[str]:
        room = self._store.get(room_id)
        if room is None or not room.log_path or not os.path.exists(room.log_path):
            return []
        try:
            with open(room.log_path) as f:
                all_lines = f.readlines()
            return [l.rstrip() for l in all_lines[-lines:]]
        except Exception as e:
            logger.warning("log_tail error: %s", e)
            return []

    def touch_activity(self, room_id: str):
        room = self._store.get(room_id)
        if room:
            room.last_active_at = time.time()
            self._store.put(room)

    def start_idle_reaper(self):
        """Start background thread that hibernates idle rooms."""
        if self._idle_thread and self._idle_thread.is_alive():
            return
        self._stop_idle.clear()
        self._idle_thread = threading.Thread(target=self._idle_loop, daemon=True)
        self._idle_thread.start()

    def stop_idle_reaper(self):
        self._stop_idle.set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _launch_multiserver(self, room: Room):
        """
        Launch stock MultiServer.py for a Standard-tier room.

        Command line (mirrors federation.py's launch_servers):
            python MultiServer.py <multidata> --port <port> --host 0.0.0.0 [--password pw]
        """
        python = sys.executable
        multiserver = os.path.join(self.repo_dir, "MultiServer.py")
        cmd = [
            python, multiserver,
            room.multidata_path,
            "--port", str(room.port),
            "--host", "0.0.0.0",
        ]
        if room.password:
            cmd += ["--password", room.password]
        return self._spawn(cmd, room)

    def _launch_peliarch(self, room: Room):
        """
        Launch the Go server (peliarch) for a Large-tier room.

        peliarch consumes a JSON bundle exported from the .archipelago by
        dump_multidata.py (not the raw multidata), so we produce that bundle once
        (cached per room) and point peliarch at it. It persists with --save and
        takes its connection password via --password.

            peliarch --host 0.0.0.0 --port <port> --multidata <bundle.apgo.json> \
                     --save <room.gosave> [--password pw]
        """
        if self._launcher is None:
            bundle = self._ensure_apgo_bundle(room)
            binary = self._peliarch_binary()
            gosave = os.path.join(os.path.dirname(room.multidata_path), f"{room.id}.gosave")
        else:
            # test/mock mode: don't run the dumper or require the built binary
            bundle, binary, gosave = "test.apgo.json", "peliarch", None

        cmd = [binary, "--host", "0.0.0.0", "--port", str(room.port), "--multidata", bundle]
        if gosave:
            cmd += ["--save", gosave]
        if room.password:
            cmd += ["--password", room.password]
        return self._spawn(cmd, room)

    def _ensure_apgo_bundle(self, room: Room) -> str:
        """
        Produce (once, cached) the Go-friendly JSON bundle for a Large-tier room by
        running archipelago-go/dump_multidata.py on the uploaded .archipelago.
        """
        bundle = os.path.join(os.path.dirname(room.multidata_path), f"{room.id}.apgo.json")
        if os.path.exists(bundle):
            return bundle
        dumper = os.path.join(self.repo_dir, "archipelago-go", "dump_multidata.py")
        if not os.path.exists(dumper):
            raise RuntimeError(f"dump_multidata.py not found at {dumper}")
        env = dict(os.environ)
        env.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
        logger.info("Exporting Large-tier bundle: %s -> %s", room.multidata_path, bundle)
        subprocess.run(
            [sys.executable, dumper, room.multidata_path, "-o", bundle],
            cwd=self.repo_dir, env=env, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        return bundle

    def _peliarch_binary(self) -> str:
        """Locate the built peliarch binary (Linux or Windows)."""
        base = os.path.join(self.repo_dir, "archipelago-go")
        for name in ("peliarch", "peliarch.exe"):
            p = os.path.join(base, name)
            if os.path.exists(p):
                return p
        raise RuntimeError(
            "peliarch binary not found in archipelago-go/ — build it first: "
            "cd archipelago-go && go build -o peliarch ."
        )

    def _spawn(self, cmd, room: Room):
        """Shared launch path: honor the test launcher override, else Popen with logs."""
        logger.info("Launch: %s", " ".join(str(c) for c in cmd))
        if self._launcher is not None:
            return self._launcher(cmd, cwd=self.repo_dir)

        env = dict(os.environ)
        env.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
        log_file = open(room.log_path, "a") if room.log_path else subprocess.DEVNULL
        return subprocess.Popen(
            cmd,
            cwd=self.repo_dir,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
        )

    def _idle_loop(self):
        while not self._stop_idle.wait(timeout=60):
            now = time.time()
            for room in self._store.all():
                if room.status != "RUNNING":
                    continue
                idle_secs = now - room.last_active_at
                if idle_secs >= room.idle_timeout:
                    logger.info(
                        "Room %s idle for %.0f s → hibernating", room.id, idle_secs
                    )
                    try:
                        self.stop_room(room.id)
                    except Exception as e:
                        logger.warning("Hibernate error for room %s: %s", room.id, e)

            # Crash-restart (with backoff)
            for room in self._store.all():
                if room.status != "RUNNING" or room._proc is None:
                    continue
                retcode = room._proc.poll()
                if retcode is not None:
                    logger.warning(
                        "Room %s process exited (rc=%s), crash_count=%d",
                        room.id, retcode, room.crash_count
                    )
                    if room.crash_count >= MAX_CRASH_RESTARTS:
                        room.status = "CRASHED"
                        room._proc = None
                        self._store.put(room)
                        logger.error(
                            "Room %s exceeded max crash restarts — parked", room.id
                        )
                    else:
                        room.crash_count += 1
                        room.status = "HIBERNATED"
                        room._proc = None
                        self._store.put(room)
                        backoff = CRASH_BACKOFF_BASE * (2 ** (room.crash_count - 1))
                        logger.info(
                            "Room %s will restart in %.0f s", room.id, backoff
                        )
                        threading.Timer(
                            backoff, self._crash_restart, args=(room.id,)
                        ).start()

    def _crash_restart(self, room_id: str):
        try:
            self.start_room(room_id)
            logger.info("Room %s restarted after crash", room_id)
        except Exception as e:
            logger.error("Room %s crash-restart failed: %s", room_id, e)
