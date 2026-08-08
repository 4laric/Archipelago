#!/usr/bin/env python3
"""
ap_loadtest.py — single-room scaling stress harness for an Archipelago MultiServer.

Goal: find the breakpoint for ONE large room (toward ~1000 slots), which is the
streamer community-sync use case. It does NOT test generation (that's a separate
fill/logic problem). It drives a real, unmodified MultiServer over the wire.

What it measures, mapped to the suspected bottlenecks:
  - probe RTT (Get->Retrieved on a dedicated connection): event-loop responsiveness.
    Periodic save() stalls and any single-loop blocking show up here as RTT spikes.
  - self-item routing latency: check a location whose item belongs to you, time
    check -> ReceivedItems. Clean causal latency for the item path.
  - check/item throughput + lag: cumulative checks sent vs items received over time.
    If the server falls behind, the gap grows -> that's the wall.
  - datastorage fan-out: N clients SetNotify a shared key, a few Set it rapidly,
    measure broadcast latency to all subscribers (the SetNotify O(subscribers) path).
  - reconnect storm: drop a fraction, rejoin all at once, measure time-to-Connected
    and the cost of the full ReceivedItems backlog resend.
  - server CPU%/RSS via psutil if --server-pid is given.

The harness owns every slot, so it can correlate self-items by location id without
needing to parse the multidata. Slot names must match the generated YAMLs
(see gen_yamls.py): <prefix><zero-padded index>, e.g. P0001..P1000.

Phases: ramp (connect) -> soak (checks) -> fanout (datastorage) -> reconnect -> report.
"""
import argparse, asyncio, json, math, statistics, sys, time, uuid
from collections import defaultdict

try:
    import websockets
except ImportError:
    sys.exit("pip install websockets --break-system-packages")

try:
    import psutil
except ImportError:
    psutil = None


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100.0
    f = math.floor(k); c = math.ceil(k)
    if f == c:
        return xs[int(k)]
    return xs[f] * (c - k) + xs[c] * (k - f)


def ms(x):
    return None if x is None else round(x * 1000, 2)


class Metrics:
    def __init__(self):
        self.probe_rtt = []                 # seconds
        self.routing_latency = []           # seconds, check -> ReceivedItems (self or cross-slot)
        self.fanout_latency = []            # seconds, Set -> SetReply at a subscriber
        self.reconnect_time = []            # seconds, reconnect -> Connected
        self.backlog_items = []             # items resent on reconnect
        self.refused = []                   # ConnectionRefused reasons
        self.errors = []
        self.checks_sent = 0
        self.items_recv = 0
        self.timeseries = []                # (t, checks_sent, items_recv, rss, cpu)


class Client:
    """One simulated player slot. Owns a persistent connection + reader task."""
    def __init__(self, uri, name, game, version, items_handling, metrics, coord, loop):
        self.uri = uri; self.name = name; self.game = game
        self.version = version; self.items_handling = items_handling
        self.m = metrics; self.coord = coord; self.loop = loop
        self.ws = None; self.reader = None
        self.slot = None
        self.missing = []                   # location ids not yet checked
        self.recv_index = 0
        self.connected = asyncio.Event()
        self.notify_keys = set()

    async def connect(self):
        # max_size=None: reconnect backlogs (ReceivedItems) can exceed the 1 MiB default frame.
        self.ws = await websockets.connect(self.uri, max_size=None, ping_interval=None)
        self.reader = asyncio.create_task(self._read())
        await self._send({
            "cmd": "Connect", "password": None, "game": self.game, "name": self.name,
            "uuid": str(uuid.uuid4()),
            "version": {"major": self.version[0], "minor": self.version[1],
                        "build": self.version[2], "class": "Version"},
            "items_handling": self.items_handling, "tags": ["Loadtest"], "slot_data": False,
        })

    async def _send(self, *cmds):
        await self.ws.send(json.dumps(list(cmds)))

    async def _read(self):
        try:
            async for raw in self.ws:
                for cmd in json.loads(raw):
                    self._dispatch(cmd)
        except Exception as e:
            self.m.errors.append(f"{self.name}:{type(e).__name__}")

    def _dispatch(self, cmd):
        c = cmd.get("cmd")
        if c == "Connected":
            self.slot = cmd.get("slot")
            self.missing = list(cmd.get("missing_locations", []))
            self.connected.set()
        elif c == "ConnectionRefused":
            self.m.refused.append(",".join(cmd.get("errors", ["?"])))
            self.connected.set()
        elif c == "ReceivedItems":
            items = cmd.get("items", [])
            now = self.loop.time()
            self.m.items_recv += len(items)
            # Cross-slot causal latency: an item we receive carries its SOURCE
            # (player = finder slot, location = where it was found). The finder
            # recorded a send-time under that same key. Works for self AND cross.
            for it in items:
                key = (it.get("player"), it.get("location"))
                t0 = self.coord.pop(key, None)
                if t0 is not None:
                    self.m.routing_latency.append(now - t0)
            self.recv_index = cmd.get("index", self.recv_index) + len(items)
        elif c == "SetReply" or c == "Retrieved":
            key = cmd.get("key")
            if key in self.notify_keys:
                # arrival of a broadcast we subscribed to: record fan-out latency
                ts = cmd.get("value", {})
                if isinstance(ts, (int, float)):
                    self.m.fanout_latency.append(self.loop.time() - ts)

    async def check_once(self):
        if not self.missing:
            return False
        loc = self.missing.pop()
        # Register send-time keyed by (our slot, location). Whoever the item
        # routes to will match this on receipt -> cross-slot routing latency.
        self.coord[(self.slot, loc)] = self.loop.time()
        await self._send({"cmd": "LocationChecks", "locations": [loc]})
        self.m.checks_sent += 1
        return True

    async def subscribe(self, key):
        self.notify_keys.add(key)
        await self._send({"cmd": "SetNotify", "keys": [key]})

    async def set_key(self, key):
        # store a send timestamp as the value; subscribers compute now-value on receipt
        await self._send({"cmd": "Set", "key": key, "default": 0, "want_reply": True,
                          "operations": [{"operation": "replace", "value": self.loop.time()}]})

    async def close(self):
        try:
            if self.reader:
                self.reader.cancel()
            if self.ws:
                await self.ws.close()
        except Exception:
            pass


class Probe:
    """Dedicated connection on a REAL reserved slot that pings datastorage
    Get->Retrieved to gauge loop health. Using a real slot guarantees the server
    serves the Get (some servers ignore datastorage from unauthenticated sockets)."""
    def __init__(self, uri, name, game, version, metrics, loop):
        self.uri = uri; self.name = name; self.game = game
        self.version = version; self.m = metrics; self.loop = loop
        self.ws = None; self.waiter = None
        self.ready = asyncio.Event()

    async def connect(self):
        self.ws = await websockets.connect(self.uri, max_size=None, ping_interval=None)
        await self.ws.send(json.dumps([{
            "cmd": "Connect", "password": None, "game": self.game, "name": self.name,
            "uuid": str(uuid.uuid4()),
            "version": {"major": self.version[0], "minor": self.version[1],
                        "build": self.version[2], "class": "Version"},
            "items_handling": 0, "tags": ["Loadtest"], "slot_data": False,
        }]))
        asyncio.create_task(self._drain())
        try:
            await asyncio.wait_for(self.ready.wait(), 30)
        except asyncio.TimeoutError:
            self.m.errors.append("probe_connect_timeout")

    async def _drain(self):
        try:
            async for raw in self.ws:
                for cmd in json.loads(raw):
                    k = cmd.get("cmd")
                    if k in ("Connected", "ConnectionRefused"):
                        if k == "ConnectionRefused":
                            self.m.refused.append("probe:" + ",".join(cmd.get("errors", ["?"])))
                        self.ready.set()
                    elif k == "Retrieved" and self.waiter and not self.waiter.done():
                        self.waiter.set_result(self.loop.time())
        except Exception:
            pass

    async def tick(self, timeout=5.0):
        self.waiter = self.loop.create_future()
        t0 = self.loop.time()
        try:
            await self.ws.send(json.dumps([{"cmd": "Get", "keys": ["__loadtest_probe__"]}]))
            t1 = await asyncio.wait_for(self.waiter, timeout)
            self.m.probe_rtt.append(t1 - t0)
        except Exception:
            self.m.probe_rtt.append(timeout)   # treat as a stall

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass


async def sampler(metrics, probe, interval, stop, server_pid):
    proc = psutil.Process(server_pid) if (psutil and server_pid) else None
    if proc:
        proc.cpu_percent(None)  # prime
    loop = asyncio.get_event_loop()
    while not stop.is_set():
        await probe.tick()
        rss = cpu = None
        if proc:
            try:
                rss = proc.memory_info().rss / (1024 * 1024)
                cpu = proc.cpu_percent(None)
            except Exception:
                pass
        metrics.timeseries.append((round(loop.time(), 3), metrics.checks_sent,
                                   metrics.items_recv, rss, cpu))
        await asyncio.sleep(interval)


async def run(args):
    loop = asyncio.get_event_loop()
    m = Metrics()
    uri = f"ws://{args.host}:{args.port}"
    ver = tuple(int(x) for x in args.version.split(","))
    all_names = [f"{args.slot_prefix}{i:0{args.slot_digits}d}" for i in range(1, args.slots + 1)]
    # Reserve the last slot for the loop-health probe; drive checks on the rest.
    probe_name = all_names[-1]
    names = all_names[:-1]

    probe = Probe(uri, probe_name, args.game, ver, m, loop)
    await probe.connect()
    stop = asyncio.Event()
    samp = asyncio.create_task(sampler(m, probe, args.probe_interval, stop, args.server_pid))

    # ---- Phase 1: connect ramp ----
    print(f"[ramp] connecting {len(names)} slots over {args.ramp_seconds}s")
    coord = {}                              # (source_slot, source_location) -> send ts
    clients = [Client(uri, n, args.game, ver, 7, m, coord, loop) for n in names]
    delay = args.ramp_seconds / max(len(names), 1)
    async def bringup(c):
        try:
            await c.connect()
            await asyncio.wait_for(c.connected.wait(), 30)
        except Exception as e:
            m.errors.append(f"bringup:{type(e).__name__}")
    tasks = []
    for c in clients:
        tasks.append(asyncio.create_task(bringup(c)))
        await asyncio.sleep(delay)
    await asyncio.gather(*tasks)
    live = [c for c in clients if c.slot is not None]
    print(f"[ramp] {len(live)}/{len(names)} connected; refused={len(m.refused)} "
          f"errors={len(m.errors)}")
    if m.refused:
        print(f"[ramp] refusal reasons (sample): {m.refused[:3]}")

    # ---- Phase 2: soak (steady checks) ----
    print(f"[soak] {args.soak_seconds}s @ {args.check_rate} checks/client/s")
    async def driver(c):
        interval = 1.0 / args.check_rate if args.check_rate > 0 else 1.0
        end = loop.time() + args.soak_seconds
        while loop.time() < end:
            if not await c.check_once():
                break
            await asyncio.sleep(interval)
    await asyncio.gather(*(driver(c) for c in live))
    print(f"[soak] checks_sent={m.checks_sent} items_recv={m.items_recv}")

    # ---- Phase 3: datastorage fan-out ----
    if args.tracker_fraction > 0:
        subs = live[:max(1, int(len(live) * args.tracker_fraction))]
        setters = live[:max(1, len(live) // 50)]
        key = "loadtest_fanout"
        print(f"[fanout] {len(subs)} subscribers, {len(setters)} setters, {args.fanout_seconds}s")
        await asyncio.gather(*(c.subscribe(key) for c in subs))
        for c in subs:
            c.notify_keys.add(key)
        await asyncio.sleep(0.3)
        end = loop.time() + args.fanout_seconds
        async def setdriver(c):
            iv = 1.0 / args.set_rate if args.set_rate > 0 else 0.2
            while loop.time() < end:
                await c.set_key(key)
                await asyncio.sleep(iv)
        await asyncio.gather(*(setdriver(c) for c in setters))
        await asyncio.sleep(0.5)
        print(f"[fanout] samples={len(m.fanout_latency)}")

    # ---- Phase 4: reconnect storm ----
    if args.reconnect_fraction > 0:
        n = max(1, int(len(live) * args.reconnect_fraction))
        victims = live[:n]
        print(f"[reconnect] dropping {n} and rejoining simultaneously")
        await asyncio.gather(*(v.close() for v in victims))
        await asyncio.sleep(0.5)
        async def rejoin(v):
            t0 = loop.time()
            fresh = Client(uri, v.name, args.game, ver, 7, m, coord, loop)
            before = m.items_recv
            try:
                await fresh.connect()
                await asyncio.wait_for(fresh.connected.wait(), 30)
                m.reconnect_time.append(loop.time() - t0)
                await asyncio.sleep(0.5)  # absorb backlog resend
                m.backlog_items.append(m.items_recv - before)
                await fresh.close()
            except Exception:
                m.errors.append("rejoin_fail")
        await asyncio.gather(*(rejoin(v) for v in victims))
        print(f"[reconnect] reconnect samples={len(m.reconnect_time)}")

    # ---- teardown ----
    stop.set()
    await samp
    await asyncio.gather(*(c.close() for c in live), return_exceptions=True)
    await probe.close()
    report(args, m, len(live))


def report(args, m, live):
    out = {
        "config": {"slots": args.slots, "live": live, "check_rate": args.check_rate,
                   "soak_seconds": args.soak_seconds, "tracker_fraction": args.tracker_fraction},
        "loop_health_probe_rtt_ms": {"p50": ms(pct(m.probe_rtt, 50)),
                                     "p95": ms(pct(m.probe_rtt, 95)),
                                     "p99": ms(pct(m.probe_rtt, 99)),
                                     "max": ms(max(m.probe_rtt) if m.probe_rtt else None),
                                     "n": len(m.probe_rtt)},
        "routing_latency_ms": {"p50": ms(pct(m.routing_latency, 50)),
                               "p95": ms(pct(m.routing_latency, 95)),
                               "p99": ms(pct(m.routing_latency, 99)),
                               "n": len(m.routing_latency)},
        "fanout_latency_ms": {"p50": ms(pct(m.fanout_latency, 50)),
                              "p95": ms(pct(m.fanout_latency, 95)),
                              "p99": ms(pct(m.fanout_latency, 99)),
                              "n": len(m.fanout_latency)},
        "reconnect": {"time_to_connected_ms_p95": ms(pct(m.reconnect_time, 95)),
                      "backlog_items_p95": pct(m.backlog_items, 95),
                      "n": len(m.reconnect_time)},
        "throughput": {"checks_sent": m.checks_sent, "items_recv": m.items_recv},
        "errors": len(m.errors), "refused": len(m.refused),
    }
    if m.errors:
        out["error_sample"] = m.errors[:5]
    if m.refused:
        out["refused_sample"] = m.refused[:5]
    print("\n===== RESULTS =====")
    print(json.dumps(out, indent=2))
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"summary": out, "timeseries": m.timeseries}, f, indent=2)
        print(f"\nwrote {args.out} (includes per-tick timeseries for plotting lag/RSS/CPU)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=38281)
    ap.add_argument("--slots", type=int, default=1000)
    ap.add_argument("--slot-prefix", default="P")
    ap.add_argument("--slot-digits", type=int, default=4)
    ap.add_argument("--game", default="Clique")
    ap.add_argument("--version", default="0,6,1", help="major,minor,build")
    ap.add_argument("--ramp-seconds", type=float, default=30.0)
    ap.add_argument("--soak-seconds", type=float, default=120.0)
    ap.add_argument("--check-rate", type=float, default=1.0,
                    help="checks per client per second")
    ap.add_argument("--tracker-fraction", type=float, default=0.2)
    ap.add_argument("--fanout-seconds", type=float, default=20.0)
    ap.add_argument("--set-rate", type=float, default=5.0)
    ap.add_argument("--reconnect-fraction", type=float, default=0.25)
    ap.add_argument("--probe-interval", type=float, default=0.5)
    ap.add_argument("--server-pid", type=int, default=None,
                    help="MultiServer PID for CPU/RSS sampling (needs psutil)")
    ap.add_argument("--out", default="results.json")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
