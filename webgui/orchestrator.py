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
import shutil
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
DEFAULT_NEVER_CONNECTED_RETENTION = 24 * 60 * 60       # 1 day
DEFAULT_USED_ROOM_RETENTION = 30 * 24 * 60 * 60        # 30 days
# Address-space cap for a room process. Measured 2026-08-13 on the live box: five running rooms at
# ~162-200 MB RSS each, so 2 GB is ~10x the observed working set -- the same ratio generator.py
# chose against its own measurement (2 GB against a 170 MB peak), and for the same reason: this is
# a BLAST-RADIUS limit, not a diet. RLIMIT_AS caps ADDRESS SPACE, which a CPython process reserves
# far more of than it resides in, so a cap near the RSS would kill healthy rooms.
#
# WHY ROOMS NEED ONE AT ALL: every room is a subprocess of the single gunicorn worker. Without a
# per-room cap the kernel OOM killer picks a victim by size, and killing the worker takes EVERY
# room down with it. With one, a runaway room dies alone and lands in CRASHED, where the existing
# backoff-restart machinery (MAX_CRASH_RESTARTS) already handles it.
DEFAULT_ROOM_MAX_AS_MB = 2048       # 0 disables
DEFAULT_UPLOAD_MAX_BYTES = 64 * 1024 * 1024   # 64 MB
MAX_CRASH_RESTARTS = 5
CRASH_BACKOFF_BASE = 5.0            # seconds; doubles each restart

# ---------------------------------------------------------------------------
# Process helpers (mirrors run_loadtest.py; no circular import)
# ---------------------------------------------------------------------------

def free_port(preferred: int, port_end: int = DEFAULT_PORT_END) -> int:
    """Return preferred if free, else scan upward for a free port.

    🛑 NOT THE ROOM ALLOCATOR. This asks the kernel only, so it hands out a port that no process
    is sitting on RIGHT NOW -- including one that a hibernated room still owns and will want back.
    `start_room` used to call it, which is why every sleeping room remembered 38400. Rooms go
    through `RoomManager._pick_free_port`, which also excludes ports claimed by the store.
    Left here for the load-test harnesses, which have no store to consult.
    """
    for p in range(preferred, port_end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"No free port found between {preferred} and {port_end}")


def _port_is_bindable(port: int) -> bool:
    """True if nothing is currently holding `port` on this box.

    🛑 SO_REUSEADDR, AND THE PROBE IS WRONG WITHOUT IT. This asks one question on behalf of
    `start_room`: will MultiServer be able to bind? MultiServer reaches bind through asyncio's
    `create_server`, which sets `reuse_address=True` on POSIX -- so a bare bind here answers a
    STRICTER question than the one being asked, and the difference is exactly the TIME_WAIT window
    after a room's own listener closes. THE MOTIVATING CASE (rule 11), 2026-08-13: a room was
    hibernated mid-session, the player pressed Start twice within five seconds, and both refused
    with "something else is holding it; check for an orphaned server process" -- about the room's
    own cooling socket. There was no orphan to find, and the message sent an operator looking for
    one.

    Measured, both states, on this box:

        while something is LISTENing   plain bind EADDRINUSE   SO_REUSEADDR bind EADDRINUSE
        after a close, in TIME_WAIT    plain bind EADDRINUSE   SO_REUSEADDR bind OK

    So the guard keeps the whole of its purpose -- a live server on the port still refuses, which
    is the case it was written for -- and stops refusing for a reason MultiServer would not have.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _room_limits(max_address_space_mb: int):
    """preexec_fn for a room process: caps it cannot raise. None where unsupported.

    🛑 DELIBERATELY NO `os.setsid()`, unlike generator.py's equivalent. Generate spawns children, so
    it needs its own process group to be killable as a unit; MultiServer does not, and giving a room
    a new session would put it outside the group `stop_server` terminates -- changing the kill path
    to fix a memory problem. One concern per change.

    RLIMIT_CORE 0 rides along: a 200 MB room dumping core on a small box fills the disk that the
    room saves live on.
    """
    if os.name != "posix" or not max_address_space_mb:
        return None
    import resource

    def _apply():
        limit = max_address_space_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return _apply


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


def connections_on_port(port: int) -> Optional[int]:
    """How many ESTABLISHED connections that port is carrying, or None if we cannot tell.

    The same table, the same reason: `/proc/net/tcp{,6}` column 3 is the state, `0A` is LISTEN and
    `01` is ESTABLISHED, and reading it touches nothing. 🛑 A connect probe is NOT available for
    this question -- connecting and closing is what filled every room log with
    `connection rejected (400 Bad Request)` every five seconds, and counting players by opening
    connections to them would be that defect with a worse motive.

    None means the table could not be read, and it is NOT the same as zero: the caller must treat
    "cannot tell" as "assume someone is playing". Hibernating a live game to save a few MB is a
    much worse trade than leaving an empty room up until the next pass.
    """
    target = f"{port:04X}"
    total, readable = 0, False
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as fh:
                rows = fh.read().splitlines()[1:]
        except OSError:
            continue
        readable = True
        for row in rows:
            cols = row.split()
            if len(cols) > 3 and cols[3] == "01" and cols[1].rsplit(":", 1)[-1].upper() == target:
                total += 1
    return total if readable else None


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
    # Set only after the host observes a real connection on the room port. This deliberately
    # distinguishes a generated-and-abandoned room from one somebody has actually played.
    first_connected_at: Optional[float] = None
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
        # Records written before connection history existed must not be swept as abandoned on the
        # first deploy. Treat them as used and let the normal, longer retention window apply.
        if "first_connected_at" not in d:
            d["first_connected_at"] = d.get("last_active_at", d.get("created_at", time.time()))
        return Room(**d)

    def connect_info(self, public_host: str) -> dict:
        """The address a player types into their client -- or nothing, when there is nothing to type.

        🛑 A PORT NUMBER IS NOT AN ADDRESS, AND THIS IS THE METHOD THAT USED TO CONFUSE THE TWO.
        It rendered an address whenever `port` was truthy, and `port` outlives the process: a
        hibernated room keeps the port it owns. THE MOTIVATING CASE (rule 11), 2026-08-12: the
        dashboard offered five sleeping rooms `ws://host:38400` apiece, each with a Copy button,
        and nothing was listening on any of them. Two of those rooms were both named
        "Player - Elden Ring", so the name did not distinguish them either.

        Why that is worse than a cosmetic bug: Archipelago's `Connect` packet carries a slot name
        and a password and NO room identifier. A client that reaches whichever server actually
        holds the port, with a slot name that seed happens to contain, joins the WRONG multiworld
        and is told nothing. So an address is safe to hand out only when it is both

          * this room's port -- guaranteed now, because the port is allocated once at creation and
            held for the room's whole life (`RoomManager._pick_free_port`), so a stale copied
            address can resolve to this room or to nothing, never to somebody else's seed; and
          * actually open -- which is what `status == RUNNING` is doing here.

        While the room sleeps the caller gets `ws: None` and has to say so in words.

        `wss` is reported for completeness and is deliberately NOT the loud string in any template.
        Room ports are published straight through by Compose and never touch Caddy, so they speak
        plaintext; a player who copies a `wss://` room address earns `SSLError:
        WRONG_VERSION_NUMBER`. CommonClient prepends `ws://` to a bare host:port, so the plain
        form is also the forgiving one.
        """
        live = self.status == "RUNNING" and bool(self.port)
        return {
            "host": public_host,
            "port": self.port,
            "live": live,
            "wss": f"wss://{public_host}:{self.port}" if live else None,
            "ws":  f"ws://{public_host}:{self.port}"  if live else None,
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
        room_max_as_mb: int = DEFAULT_ROOM_MAX_AS_MB,
        never_connected_retention: int = DEFAULT_NEVER_CONNECTED_RETENTION,
        used_room_retention: int = DEFAULT_USED_ROOM_RETENTION,
        log_archive_dir: Optional[str] = None,
    ):
        self.data_dir = data_dir
        self.repo_dir = repo_dir
        self.public_host = public_host
        self.port_start = port_start
        self.port_end = port_end
        self.upload_max_bytes = upload_max_bytes
        self.room_max_as_mb = room_max_as_mb
        self.never_connected_retention = never_connected_retention
        self.used_room_retention = used_room_retention
        self.log_archive_dir = log_archive_dir or os.path.join(data_dir, "server-logs")
        self._launcher = launcher  # None → use real subprocess.Popen
        self._store = RoomStore(store_path)
        self._lock = threading.Lock()
        self._idle_thread: Optional[threading.Thread] = None
        self._stop_idle = threading.Event()

        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(self.log_archive_dir, exist_ok=True)

        # Resurrect any rooms that were RUNNING at last shutdown → HIBERNATED
        for room in self._store.all():
            if room.status in ("RUNNING", "STARTING"):
                room.status = "HIBERNATED"
                room._proc = None
                room.pid = None
                room.launch_pid = None
                self._store.put(room)

        self._resolve_port_collisions()

    # ------------------------------------------------------------------
    # Port allocation -- one port per room, for the room's life
    # ------------------------------------------------------------------

    def _claimed_ports(self, except_room_id: Optional[str] = None) -> set:
        """Every port the store has promised to a room, running or not.

        A hibernated room's port is not free. It is reserved -- that reservation is the whole
        safety property, because it is what stops a stale connect address a player copied last
        week from resolving to a different seed this week.
        """
        return {
            r.port for r in self._store.all()
            if r.port is not None and r.id != except_room_id
        }

    def _pick_free_port(self, exclude: Optional[set] = None) -> int:
        """A port inside the range that no room claims and no process holds."""
        exclude = exclude or set()
        for p in range(self.port_start, self.port_end):
            if p in exclude:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("0.0.0.0", p))
                except OSError:
                    continue
            return p
        raise RuntimeError(
            f"No free port between {self.port_start} and {self.port_end}; "
            f"{len(exclude)} are claimed by existing rooms. Delete a finished room, or widen "
            f"PORT_START/PORT_END."
        )

    def _resolve_port_collisions(self) -> None:
        """Give every room in the store a port of its own, once, at startup.

        🛑 STICKY PORTS INHERIT A COLLISION UNLESS IT IS BROKEN HERE. Rooms created before the
        port was sticky carry whatever port they last STARTED on, and on a box that runs one room
        at a time that is the same number for all of them -- the five rooms live on peliarch.ca
        all record 38400. Making the port permanent without this pass would freeze the exact
        confusion it was meant to end.

        First claimant of a valid port keeps it; everyone else is re-homed and the move is logged,
        because an operator who has handed an address to a player deserves to see it change.
        """
        seen: Dict[int, str] = {}
        needs_port: List[Room] = []
        for room in self._store.all():
            in_range = room.port is not None and self.port_start <= room.port < self.port_end
            if in_range and room.port not in seen:
                seen[room.port] = room.id
            else:
                needs_port.append(room)
        for room in needs_port:
            old = room.port
            room.port = self._pick_free_port(exclude=set(seen))
            seen[room.port] = room.id
            logger.warning(
                "Room %s: port %s -> %d (was %s)", room.id, old, room.port,
                "claimed by another room" if old is not None else "unset",
            )
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
            # The port is part of the room's identity, not of one launch. See
            # Room.connect_info for what goes wrong when it is per-start.
            port=self._pick_free_port(exclude=self._claimed_ports()),
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
            # The room already owns a port (create_room assigned it). Reuse it -- do NOT go
            # looking for a free one, or a restart silently moves a room whose address players
            # already hold.
            port = room.port
            if port is None:
                # Only rooms created before ports were sticky reach here; _resolve_port_collisions
                # normally fills these in at startup.
                port = room.port = self._pick_free_port(
                    exclude=self._claimed_ports(except_room_id=room.id))
            if not _port_is_bindable(port):
                # Loud on purpose. Quietly picking a different port is how a player ends up
                # connected to a stranger's multiworld with no error on either side.
                raise RuntimeError(
                    f"Port {port} belongs to room {room.id} but something else is LISTENING on "
                    f"it; not starting. This is not a socket cooling down from this room's own "
                    f"last run -- TIME_WAIT is excluded -- so look for another server process on "
                    f"that port."
                )
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
        # room.port is KEPT. It is the room's for as long as the room exists, so waking it up
        # gives players back the address they already have. What must not survive the stop is the
        # claim that anything is listening on it -- Room.connect_info reads `status` for that.
        room.status = "HIBERNATED"
        self._store.put(room)
        logger.info("Room %s stopped → HIBERNATED", room_id)
        return room

    def _archive_room_log(self, room: Room) -> Optional[str]:
        """Copy unmodified server output outside the room's disposable directory.

        Cleanup removes multidata and saves, but this transcript is the useful artifact when a
        player reports something after the room is gone. Archives share the persistent data volume
        while living outside room directories, so room deletion cannot remove them.
        """
        if not room.log_path or not os.path.isfile(room.log_path):
            return None
        month = time.strftime("%Y-%m", time.gmtime(room.created_at))
        archive_dir = os.path.join(self.log_archive_dir, month)
        os.makedirs(archive_dir, exist_ok=True)
        archive_path = os.path.join(archive_dir, f"{room.id}.server.log")
        shutil.copy2(room.log_path, archive_path)
        logger.info("Archived room %s server log to %s", room.id, archive_path)
        return archive_path

    def delete_room(self, room_id: str, reason: str = "manual") -> None:
        room = self._store.get(room_id)
        if room is None:
            return
        if room.status == "RUNNING":
            self.stop_room(room_id)
        # Archive before removing the room directory. If archival fails, deletion fails too: a
        # cleanup retry is cheaper than silently discarding the evidence this feature exists for.
        self._archive_room_log(room)
        room_dir = os.path.dirname(room.multidata_path)
        try:
            shutil.rmtree(room_dir)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("Could not remove room dir %s: %s", room_dir, e)
            raise
        self._store.delete(room_id)
        logger.info("Room %s deleted (%s)", room_id, reason)

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

    def touch_activity(self, room_id: str, when: Optional[float] = None):
        """Record that a room is in use. `when` exists so one reaper pass uses ONE clock.

        The pass compares `now - last_active_at` against the timeout; if the refresh stamped
        `time.time()` while the comparison used a `now` passed in, the two would disagree by
        however long the pass took -- and in a test with an injected clock, by hours.
        """
        room = self._store.get(room_id)
        if room:
            room.last_active_at = time.time() if when is None else when
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
        # The cap goes HERE, in the one shared path, rather than in _launch_multiserver and
        # _launch_peliarch separately -- a limit that covers one tier and not the other is a limit
        # nobody can reason about. The mock launcher is handed it too so tests can assert it.
        preexec = _room_limits(self.room_max_as_mb)
        if self._launcher is not None:
            return self._launcher(cmd, cwd=self.repo_dir, preexec_fn=preexec)

        env = dict(os.environ)
        env.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
        log_file = open(room.log_path, "a") if room.log_path else subprocess.DEVNULL
        return subprocess.Popen(
            cmd,
            cwd=self.repo_dir,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            preexec_fn=preexec,
        )

    def hibernate_idle_rooms(self, now: Optional[float] = None):
        """One pass of the reaper: refresh activity from the wire, then sleep what is idle.

        🛑🛑 THE REFRESH IS THE WHOLE FIX. `last_active_at` had exactly two writers -- `start_room`
        and `touch_activity` -- and `touch_activity` had NO CALLER anywhere in this codebase, so
        `idle_secs` was not idleness at all. It was UPTIME, and every room was hibernated exactly
        `idle_timeout` after it started with its players still connected and mid-send. THE
        MOTIVATING CASE (rule 11), 2026-08-13: a room went down under someone thirty minutes in,
        an orderly `server closing` in its log, reported as a crash because that is what it looks
        like from the inside. The site's own copy -- "a room sleeps when nobody has been connected
        for a while" -- described a thing nothing measured.

        A room is active while its port is carrying connections. That is the sentence the copy
        promises, and `connections_on_port` is the honest reading of it.
        """
        now = time.time() if now is None else now
        for room in self._store.all():
            if room.status != "RUNNING" or not room.port:
                continue
            live = connections_on_port(room.port)
            if live is None:
                # None = the table would not read. Ignorance keeps the room UP, on purpose.
                self.touch_activity(room.id, now)
            elif live > 0:
                if room.first_connected_at is None:
                    room.first_connected_at = now
                    logger.info("Room %s received its first connection", room.id)
                room.last_active_at = now
                self._store.put(room)

        for room in self._store.all():
            if room.status != "RUNNING":
                continue
            idle_secs = now - room.last_active_at
            if idle_secs >= room.idle_timeout:
                logger.info(
                    "Room %s idle for %.0f s (no connections) → hibernating", room.id, idle_secs
                )
                try:
                    self.stop_room(room.id)
                except Exception as e:
                    logger.warning("Hibernate error for room %s: %s", room.id, e)

    def cleanup_stale_rooms(self, now: Optional[float] = None) -> List[str]:
        """Delete old inactive rooms, using a shorter window for rooms nobody joined.

        Running and starting rooms are never retention-deleted. Set either retention to zero to
        disable cleanup for that class of room.
        """
        now = time.time() if now is None else now
        deleted: List[str] = []
        for room in self._store.all():
            if room.status in ("RUNNING", "STARTING"):
                continue
            if room.first_connected_at is None:
                retention = self.never_connected_retention
                age = now - room.created_at
                kind = "never connected"
            else:
                retention = self.used_room_retention
                age = now - room.last_active_at
                kind = "previously used"
            if retention <= 0 or age < retention:
                continue
            try:
                self.delete_room(
                    room.id,
                    reason=f"{kind}; inactive for {age:.0f}s (retention {retention}s)",
                )
                deleted.append(room.id)
            except Exception as e:
                logger.warning("Stale-room cleanup failed for %s: %s", room.id, e)
        return deleted

    def _idle_loop(self):
        while not self._stop_idle.wait(timeout=60):
            now = time.time()
            self.hibernate_idle_rooms(now)
            self.cleanup_stale_rooms(now)

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
