# Peliarch Web GUI

Self-host edition — free, single-box, donation-supported.

Upload a `.archipelago` multidata file, get a connect address, watch it run.

---

## The one hard constraint you must understand

**In Archipelago, a room's identity IS its `host:port`.**

The AP client connects to a bare WebSocket — `wss://yourhost:PORT` — and the server
immediately sends `RoomInfo`. There is no room-ID in the handshake. This means:

| Thing | Address |
|---|---|
| Management web page | `http://yourhost:8080/room/<id>` |
| **Game connection (what players use)** | **`wss://yourhost:PORT`** |

The UI shows both clearly. Never conflate them. The `wss://` address is the headline
element of every room page.

---

## Quick start (single box)

```bash
# 1. Install Flask (if not already)
pip install flask

# 2. From the Archipelago repo root:
PUBLIC_HOST=your.domain.or.ip python webgui/app.py
```

The server starts on port 8080. Put Caddy or Traefik in front for TLS (`wss://`).

---

## Configuration

All options can be set as environment variables:

| Variable | Default | Description |
|---|---|---|
| `PUBLIC_HOST` | `localhost` | Hostname/IP shown to players in the wss:// address |
| `PORT_START` | `38400` | Start of room port allocation range |
| `PORT_END` | `38600` | End of room port allocation range |
| `DATA_DIR` | `webgui/room_data/` | Where multidata, saves, and logs are stored |
| `STORE_PATH` | `webgui/rooms.json` | Room record persistence file |
| `REPO_DIR` | repo root | Path to Archipelago checkout (where MultiServer.py is) |
| `DONATION_URL` | `https://buymeacoffee.com/your-handle` | **Set this to your real tip-jar URL** |
| `UPLOAD_MAX_BYTES` | 67108864 (64 MB) | Max .archipelago upload size |
| `LOG_TAIL_LINES` | 200 | Lines shown in the log tail on room page |

### Setting your donation URL

Edit the constant in `webgui/app.py`:

```python
DONATION_URL = "https://buymeacoffee.com/your-handle"
```

Or set the environment variable `DONATION_URL` at runtime. The link appears in the
top navigation bar and the page footer on every page.

---

## Ingress / TLS setup (Caddy example)

Each running room binds its own port. You need TLS termination that forwards each
room port through to the backend. Caddy's simplest approach is a wildcard port forward:

```caddyfile
your.domain {
    # Web UI
    handle /room/* { reverse_proxy localhost:8080 }
    handle { reverse_proxy localhost:8080 }
}

# Per-room port — repeat or use a range approach
your.domain:38400 { reverse_proxy localhost:38400 }
your.domain:38401 { reverse_proxy localhost:38401 }
# ... up to PORT_END
```

Alternatively, use `--tls-alpn` termination at Traefik with a TCP passthrough per port.

---

## Room lifecycle

```
UPLOADED ──auto-start──► STARTING ──ready──► RUNNING
   ▲                                         │
   │                         idle > timeout  │
   │                                         ▼
 (reconnect wakes) ◄──── HIBERNATED ◄──save──┘
                                             │ crash
                                  RUNNING ◄─restart─ CRASHED (after 5 retries)
```

- **RUNNING**: process up, port allocated, players can connect.
- **HIBERNATED**: no connections for `idle_timeout` minutes → process killed, port freed.
  Connecting wakes it in a few seconds.
- **CRASHED**: process exited unexpectedly; auto-restarts with exponential backoff (5×
  max). After 5 failures the room is parked — use the Start button to retry.

---

## Security notes

### Upload validation
- **Size cap** enforced both at Flask (`MAX_CONTENT_LENGTH`) and in `orchestrator.py`
  (`validate_archipelago_upload`). Default 64 MB.
- **ZIP structure check**: every upload must be a valid ZIP with no path-traversal entries
  (`..` or leading `/`).
- **No plain pickle**: the orchestrator never calls `pickle.load` on uploaded data.
  MultiServer itself uses `restricted_loads` (from `Utils.py`) when loading multidata.

### Process isolation
Each room runs as a subprocess. For production use, add cgroup or container limits
(CPU/mem/fd) per room. See `HOSTING.md §5`.

### Room limits
- Upload size cap: 64 MB (configurable).
- Port range: 38400–38600 by default (200 rooms max simultaneously running).
- Idle hibernate at 30 minutes by default (configurable per room).

---

## Running tests

```bash
# From repo root
pip install flask pytest
python -m pytest webgui/test_app.py -v
```

Tests use Flask's synchronous test client. No real MultiServer processes are spawned —
the orchestrator is replaced with a `MockManager` that validates uploads and tracks
calls without touching the filesystem or network.

---

## Per-room launch command

The orchestrator launches MultiServer as:

```
python MultiServer.py <multidata_path> --port <PORT> --host 0.0.0.0 [--password <pw>]
```

This mirrors `federation.py`'s `launch_servers()`. The `SKIP_REQUIREMENTS_UPDATE=1`
environment variable is set to prevent ModuleUpdate from pip-installing on every launch.

**Assumption to verify**: `MultiServer.py` is in the repo root (`REPO_DIR`). If your
setup uses a different entry point (e.g. `python -m MultiServer`), update
`RoomManager._launch_multiserver()` in `orchestrator.py`.

---

## MVP vs roadmap

**MVP (what's built here):**
- Upload → host → room page with copy-able wss:// address
- Status pills (RUNNING / HIBERNATED / CRASHED / STARTING)
- Live log tail via SSE
- Start / Stop / Delete / Set-password controls
- Idle hibernate + crash-restart with backoff
- Restricted unpickling + size caps on uploads
- Donation link in nav + footer

**Roadmap:**
- Multi-tenant auth (signup/login, room ownership)
- Large tier (peliarch + real-multidata loader)
- Federated tier (islands + bridge + director)
- Player slot table (read from server state via health probe)
- Multi-node scheduling
