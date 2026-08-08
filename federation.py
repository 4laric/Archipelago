#!/usr/bin/env python3
"""
federation.py - orchestrate an "islands of n" federation of stock MultiServers,
linked by a thin spectator bridge that relays chat and aggregates presence.

See FEDERATION.md for the design. This is Strategy A (self-contained islands +
social bridge): each island is an independent multiworld of n players; cross-island
is social only (no cross-island item routing). It uses stock MultiServer unmodified.

Pipeline:
  for each of K islands:
    gen_yamls (deterministic names I<k>P####, n players + 1 reserved bridge slot)
    Generate.py --spoiler 0  -> island multidata
    launch MultiServer on its own port
  then run the bridge: connect a Tracker/TextOnly spectator to each island's
  reserved bridge slot, relay chat between islands, aggregate _read_client_status.

Reserved slot: island "size n" = n player slots + 1 bridge slot (the last name).

Examples
  python federation.py --total 1000 --island-size 150          # 7 islands of 150
  python federation.py --total 300 --island-size 100 --dry-run
  python federation.py --islands 2 --island-size 30 --soak 0    # tiny smoke (servers only)
"""
import argparse, asyncio, json, os, shutil, sys, time, uuid

# reuse the runner's process helpers (no main() runs on import)
try:
    from run_loadtest import free_port, detect_version, resolve_server_pid, stop_server, newest_multidata
except Exception as e:
    sys.exit(f"federation.py expects run_loadtest.py beside it ({e})")

try:
    import websockets
except ImportError:
    sys.exit("pip install websockets")

HERE = os.path.dirname(os.path.abspath(__file__))
# island scratch (multidata, player yamls); override with $FED_DIR to keep it out
# of the checkout or point it at scratch storage.
FED_DIR = os.environ.get("FED_DIR") or os.path.join(HERE, "federation")


def log(msg):
    print(f"[fed] {msg}", flush=True)


class Island:
    def __init__(self, idx, size, game, digits=4):
        self.idx = idx
        self.size = size                       # player slots
        self.game = game
        self.digits = digits
        self.prefix = f"I{idx:02d}P"
        self.bridge_slot_name = f"{self.prefix}{size + 1:0{digits}d}"  # reserved last slot
        self.dir = os.path.join(FED_DIR, f"island_{idx:02d}")
        self.players_dir = os.path.join(self.dir, "players")
        self.out_dir = os.path.join(self.dir, "out")
        self.port = None
        self.multidata = None
        self.srv = None
        self.server_pid = None


# ----------------------------- generation -----------------------------

def generate_islands(islands, py, spoiler, dry):
    for isl in islands:
        log(f"island {isl.idx}: {isl.size} players (+1 bridge) game={isl.game} prefix={isl.prefix}")
        if not dry:
            shutil.rmtree(isl.dir, ignore_errors=True)
            os.makedirs(isl.players_dir, exist_ok=True)
        # n players + 1 reserved bridge slot
        cmd = [py, os.path.join(HERE, "gen_yamls.py"), "--count", str(isl.size + 1),
               "--game", isl.game, "--prefix", isl.prefix, "--digits", str(isl.digits),
               "--out", isl.players_dir]
        log("$ " + " ".join(cmd))
        if not dry:
            import subprocess
            subprocess.check_call(cmd)
            gen = [py, os.path.join(HERE, "Generate.py"),
                   "--player_files_path", isl.players_dir,
                   "--outputpath", isl.out_dir, "--spoiler", str(spoiler)]
            log("$ " + " ".join(gen))
            subprocess.check_call(gen)
            isl.multidata = newest_multidata(isl.out_dir)
            if not isl.multidata:
                sys.exit(f"island {isl.idx}: no multidata produced")
            log(f"island {isl.idx}: multidata={os.path.basename(isl.multidata)}")


# ----------------------------- server launch -----------------------------

def launch_servers(islands, py, base_port, dry):
    import subprocess, socket
    for isl in islands:
        isl.port = free_port(base_port + isl.idx) if not dry else base_port + isl.idx
        cmd = [py, os.path.join(HERE, "MultiServer.py"), isl.multidata or "<multidata>",
               "--port", str(isl.port), "--host", "0.0.0.0"]
        log(f"island {isl.idx} -> port {isl.port}:  $ " + " ".join(str(c) for c in cmd))
        if dry:
            continue
        isl.srv = subprocess.Popen(cmd, cwd=HERE)
    if dry:
        return
    # wait for all ports, resolve real pids
    for isl in islands:
        end = time.time() + 90
        ok = False
        while time.time() < end:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                if s.connect_ex(("127.0.0.1", isl.port)) == 0:
                    ok = True
                    break
            time.sleep(0.5)
        if not ok:
            shutdown(islands)
            sys.exit(f"island {isl.idx} server never opened port {isl.port}")
        isl.server_pid = resolve_server_pid(isl.srv.pid, isl.port)
    log(f"all {len(islands)} island servers up")


def shutdown(islands):
    for isl in islands:
        stop_server(isl.srv)


# ----------------------------- the bridge -----------------------------

class Bridge:
    """One spectator (Tracker/TextOnly) connection per island. Relays chat across
    islands and aggregates per-slot client status into a global board."""

    def __init__(self, islands, version):
        self.islands = islands
        self.version = version
        self.conns = {}                    # idx -> websocket
        self.my_slot = {}                  # idx -> our own slot number (loop guard)
        self.status = {}                   # (idx, slot) -> client status int
        self.ready = {}                    # idx -> asyncio.Event

    async def connect_one(self, isl):
        uri = f"ws://127.0.0.1:{isl.port}"
        ws = await websockets.connect(uri, max_size=None, ping_interval=None)
        self.conns[isl.idx] = ws
        self.ready[isl.idx] = asyncio.Event()
        await ws.send(json.dumps([{
            "cmd": "Connect", "password": None, "game": isl.game,
            "name": isl.bridge_slot_name, "uuid": str(uuid.uuid4()),
            "version": {"major": self.version[0], "minor": self.version[1],
                        "build": self.version[2], "class": "Version"},
            "items_handling": 0, "tags": ["Tracker", "TextOnly", "Bridge"],
            "slot_data": False,
        }]))
        asyncio.create_task(self._reader(isl, ws))

    async def _reader(self, isl, ws):
        try:
            async for raw in ws:
                for cmd in json.loads(raw):
                    await self._dispatch(isl, cmd)
        except Exception as e:
            log(f"island {isl.idx} bridge reader closed: {type(e).__name__}")

    async def _dispatch(self, isl, cmd):
        c = cmd.get("cmd")
        if c == "Connected":
            self.my_slot[isl.idx] = cmd.get("slot")
            self.ready[isl.idx].set()
            # subscribe to every player's status key for presence aggregation
            keys = [f"_read_client_status_0_{s}" for s in range(1, isl.size + 1)]
            await ws_send(self.conns[isl.idx], {"cmd": "SetNotify", "keys": keys})
            await ws_send(self.conns[isl.idx], {"cmd": "Get", "keys": keys})
        elif c == "ConnectionRefused":
            log(f"island {isl.idx} bridge REFUSED: {cmd.get('errors')}")
            self.ready[isl.idx].set()
        elif c == "PrintJSON" and cmd.get("type") == "Chat":
            await self._relay_chat(isl, cmd)
        elif c in ("Retrieved", "SetReply"):
            self._absorb_status(isl, cmd)

    async def _relay_chat(self, isl, cmd):
        sender = cmd.get("slot")
        # loop guard: never re-relay our own bridge slot's messages (the echo of a relay)
        if sender == self.my_slot.get(isl.idx):
            return
        text = cmd.get("message", "")
        if not text:
            return
        out = f"[I{isl.idx:02d}] {text}"
        for other in self.islands:
            if other.idx == isl.idx:
                continue
            ws = self.conns.get(other.idx)
            if ws:
                await ws_send(ws, {"cmd": "Say", "text": out})

    def _absorb_status(self, isl, cmd):
        # Retrieved: {"keys": {key: value}}  |  SetReply: {"key":..., "value":...}
        items = cmd.get("keys") or {cmd.get("key"): cmd.get("value")}
        for k, v in items.items():
            if k and k.startswith("_read_client_status_"):
                try:
                    slot = int(k.rsplit("_", 1)[-1])
                    self.status[(isl.idx, slot)] = v
                except ValueError:
                    pass

    def board(self):
        # ClientStatus: 0 unknown, 10 connected, 20 ready, 30 playing, 40 goal
        goal = sum(1 for v in self.status.values() if v == 40)
        playing = sum(1 for v in self.status.values() if v in (20, 30))
        seen = len(self.status)
        return f"presence: seen={seen} active={playing} finished={goal}"

    async def run(self, board_interval):
        await asyncio.gather(*(self.connect_one(i) for i in self.islands))
        for isl in self.islands:
            try:
                await asyncio.wait_for(self.ready[isl.idx].wait(), 30)
            except asyncio.TimeoutError:
                log(f"island {isl.idx} bridge connect timed out")
        log(f"bridge linked {len(self.conns)} islands; relaying chat + presence")
        while True:
            await asyncio.sleep(board_interval)
            log(self.board())


async def ws_send(ws, obj):
    await ws.send(json.dumps([obj]))


# ----------------------------- main -----------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--total", type=int, help="total players; islands = ceil(total/size)")
    g.add_argument("--islands", type=int, help="explicit island count")
    ap.add_argument("--island-size", type=int, default=150, help="player slots per island")
    ap.add_argument("--game", default="ChecksFinder")
    ap.add_argument("--version", default=None, help="major,minor,build (default: this checkout)")
    ap.add_argument("--base-port", type=int, default=38300)
    ap.add_argument("--spoiler", type=int, default=0)
    ap.add_argument("--board-interval", type=float, default=15.0)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--skip-generate", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.islands:
        k = args.islands
    elif args.total:
        k = (args.total + args.island_size - 1) // args.island_size
    else:
        ap.error("need --total or --islands")

    version = tuple(int(x) for x in (args.version or detect_version()).split(","))
    islands = [Island(i, args.island_size, args.game) for i in range(k)]
    log(f"federation: {k} islands x {args.island_size} players "
        f"(+1 bridge each) = {k * args.island_size} players, game={args.game}, ver={version}")

    if not args.skip_generate:
        generate_islands(islands, args.python, args.spoiler, args.dry_run)
    else:
        for isl in islands:
            isl.multidata = newest_multidata(isl.out_dir)

    launch_servers(islands, args.python, args.base_port, args.dry_run)

    if args.dry_run:
        log("dry-run: would now start the bridge spectator on each island and relay.")
        log("ports: " + ", ".join(f"I{i.idx:02d}={i.port}" for i in islands))
        return

    log("island ports: " + ", ".join(f"I{i.idx:02d}=:{i.port}" for i in islands))
    log("players connect to their island's port; Ctrl+C to tear down")
    bridge = Bridge(islands, version)
    try:
        asyncio.run(bridge.run(args.board_interval))
    except KeyboardInterrupt:
        pass
    finally:
        shutdown(islands)
        log("all islands stopped")


if __name__ == "__main__":
    main()
