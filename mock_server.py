#!/usr/bin/env python3
"""
mock_server.py — a tiny fake AP MultiServer, ONLY for validating ap_loadtest.py.

It implements the slice of the protocol the harness exercises:
  RoomInfo, Connect->Connected (with synthetic missing_locations),
  LocationScouts->LocationInfo (self-items), LocationChecks->ReceivedItems (self),
  Get->Retrieved, SetNotify, Set->SetReply broadcast to subscribers.

--stall-every / --stall-ms injects a real, blocking time.sleep() on the event loop
to simulate the periodic save() stop-the-world. The harness's probe RTT should spike
in lockstep -> proves the measurement works. This is NOT a real server; it makes no
claim about real performance. Real runs go against an actual MultiServer.
"""
import argparse, asyncio, json, time
import websockets

LOCS_PER_SLOT = 50  # synthetic locations each slot owns (all self-routed here)


class Hub:
    def __init__(self):
        self.subs = {}            # key -> set(websocket)
        self.store = {}           # key -> value
        self.slot_ws = {}         # slot -> websocket (for cross-slot routing)
        self.next_slot = 1

    def sub(self, ws, key):
        self.subs.setdefault(key, set()).add(ws)


async def handler(ws, hub, args):
    slot = None
    base = None
    await ws.send(json.dumps([{"cmd": "RoomInfo",
                               "version": {"major": 0, "minor": 6, "build": 1, "class": "Version"},
                               "tags": [], "password": False, "permissions": {},
                               "games": ["Clique"], "datapackage_checksums": {}}]))
    try:
        async for raw in ws:
            for cmd in json.loads(raw):
                c = cmd.get("cmd")
                if c == "Connect":
                    slot = hub.next_slot; hub.next_slot += 1
                    base = slot * 100000
                    hub.slot_ws[slot] = ws
                    missing = list(range(base, base + LOCS_PER_SLOT))
                    await ws.send(json.dumps([{"cmd": "Connected", "team": 0, "slot": slot,
                        "players": [], "missing_locations": missing, "checked_locations": [],
                        "slot_data": {}, "slot_info": {}, "hint_points": 0}]))
                elif c == "LocationScouts":
                    info = [{"item": l, "location": l, "player": slot, "flags": 0}
                            for l in cmd.get("locations", [])]
                    await ws.send(json.dumps([{"cmd": "LocationInfo", "locations": info}]))
                elif c == "LocationChecks":
                    # Route each checked location's item to a DIFFERENT slot (S -> S+1),
                    # delivered on that slot's connection. Item carries its source
                    # (player=finder slot, location=source loc) so the harness can pair
                    # the finder's send-time with the recipient's receive-time cross-slot.
                    for l in cmd.get("locations", []):
                        target = (slot % (hub.next_slot - 1)) + 1 if hub.next_slot > 1 else slot
                        tws = hub.slot_ws.get(target, ws)
                        try:
                            await tws.send(json.dumps([{"cmd": "ReceivedItems", "index": 0,
                                "items": [{"item": l, "location": l, "player": slot, "flags": 0}]}]))
                        except Exception:
                            pass
                elif c == "Get":
                    keys = {k: hub.store.get(k) for k in cmd.get("keys", [])}
                    await ws.send(json.dumps([{"cmd": "Retrieved", "keys": keys}]))
                elif c == "SetNotify":
                    for k in cmd.get("keys", []):
                        hub.sub(ws, k)
                elif c == "Set":
                    key = cmd.get("key")
                    val = cmd.get("operations", [{}])[-1].get("value")
                    hub.store[key] = val
                    msg = json.dumps([{"cmd": "SetReply", "key": key, "value": val,
                                       "original_value": None}])
                    # broadcast to all subscribers — the O(subscribers) fan-out path
                    dead = []
                    for sub in hub.subs.get(key, ()):
                        try:
                            await sub.send(msg)
                        except Exception:
                            dead.append(sub)
                    for d in dead:
                        hub.subs[key].discard(d)
    except Exception:
        pass


async def stall_injector(args):
    if args.stall_every <= 0:
        return
    while True:
        await asyncio.sleep(args.stall_every)
        time.sleep(args.stall_ms / 1000.0)  # block the loop, like a synchronous save


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=38281)
    ap.add_argument("--stall-every", type=float, default=0.0, help="seconds between injected stalls")
    ap.add_argument("--stall-ms", type=float, default=300.0, help="stall duration ms")
    args = ap.parse_args()
    hub = Hub()
    async with websockets.serve(lambda ws: handler(ws, hub, args), args.host, args.port,
                                max_size=None, ping_interval=None):
        print(f"mock server on ws://{args.host}:{args.port} "
              f"(stall_every={args.stall_every}s stall_ms={args.stall_ms})")
        await stall_injector(args)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
