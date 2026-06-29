#!/usr/bin/env python3
"""
federation_smoke.py - end-to-end smoke test for the islands federation.

Stands up 2 tiny islands + the bridge, connects a real player to each, has the
player on island 0 send a chat line, and asserts it crosses the bridge and arrives
at the player on island 1 (prefixed [I00]). Also checks the loop-guard didn't echo.

Exit code 0 = PASS, 1 = FAIL. Safe to run in CI / before a real federation launch.

  python federation_smoke.py                 # 2 islands x 3 players
  python federation_smoke.py --island-size 5 --base-port 38400
"""
import argparse, asyncio, json, sys, time, uuid
import federation as F
from run_loadtest import detect_version

try:
    import websockets
except ImportError:
    sys.exit("pip install websockets")


async def player(uri, game, name, version, tags, inbox=None):
    """Minimal real client: Connect as a named slot, optionally record Chat lines."""
    ws = await websockets.connect(uri, max_size=None, ping_interval=None)
    connected = asyncio.Event()
    refused = []

    async def reader():
        try:
            async for raw in ws:
                for cmd in json.loads(raw):
                    c = cmd.get("cmd")
                    if c == "Connected":
                        connected.set()
                    elif c == "ConnectionRefused":
                        refused.append(cmd.get("errors"))
                        connected.set()
                    elif c == "PrintJSON" and cmd.get("type") == "Chat" and inbox is not None:
                        inbox.append(cmd.get("message", ""))
        except Exception:
            pass

    asyncio.create_task(reader())
    await ws.send(json.dumps([{
        "cmd": "Connect", "password": None, "game": game, "name": name,
        "uuid": str(uuid.uuid4()),
        "version": {"major": version[0], "minor": version[1], "build": version[2],
                    "class": "Version"},
        "items_handling": 0, "tags": tags, "slot_data": False,
    }]))
    await asyncio.wait_for(connected.wait(), 30)
    if refused:
        raise RuntimeError(f"{name} connect refused: {refused[0]}")
    return ws


async def run_test(islands, version, game):
    # bring up the bridge (reuse the real relay logic; connect only, skip board loop)
    bridge = F.Bridge(islands, version)
    await asyncio.gather(*(bridge.connect_one(i) for i in islands))
    for isl in islands:
        await asyncio.wait_for(bridge.ready[isl.idx].wait(), 30)
    print(f"[smoke] bridge linked {len(bridge.conns)} islands")

    # a listener on island 1, a talker on island 0 (real player slots)
    inbox = []
    isl0, isl1 = islands[0], islands[1]
    listener = await player(f"ws://127.0.0.1:{isl1.port}", game,
                            f"{isl1.prefix}0001", version, ["TextOnly"], inbox)
    talker = await player(f"ws://127.0.0.1:{isl0.port}", game,
                          f"{isl0.prefix}0001", version, [])
    await asyncio.sleep(0.5)  # let subscriptions settle

    nonce = uuid.uuid4().hex[:8]
    msg = f"fed-smoke-{nonce}"
    print(f"[smoke] island 0 player says: {msg!r}")
    await talker.send(json.dumps([{"cmd": "Say", "text": msg}]))

    # expect "[I00] fed-smoke-<nonce>" to arrive at the island-1 listener via the bridge
    deadline = time.time() + 15
    crossed = None
    while time.time() < deadline:
        for m in inbox:
            if nonce in m and m.startswith("[I00]"):
                crossed = m
                break
        if crossed:
            break
        await asyncio.sleep(0.2)

    for ws in (listener, talker):
        await ws.close()
    for w in bridge.conns.values():
        await w.close()

    if not crossed:
        print(f"[smoke] FAIL: chat did not cross the bridge. island-1 inbox={inbox!r}")
        return False
    # loop-guard: the relayed line must appear exactly once on island 1 (no echo storm)
    echoes = sum(1 for m in inbox if nonce in m)
    print(f"[smoke] island 1 received: {crossed!r}  (occurrences={echoes})")
    if echoes != 1:
        print(f"[smoke] FAIL: loop-guard breach, message seen {echoes}x")
        return False
    print("[smoke] PASS: chat crossed exactly once, prefixed by origin island")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--island-size", type=int, default=3)
    ap.add_argument("--game", default="ChecksFinder")
    ap.add_argument("--version", default=None)
    ap.add_argument("--base-port", type=int, default=38400)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--keep-generate", action="store_true",
                    help="reuse existing island multidata if present")
    args = ap.parse_args()

    version = tuple(int(x) for x in (args.version or detect_version()).split(","))
    islands = [F.Island(0, args.island_size, args.game),
               F.Island(1, args.island_size, args.game)]
    print(f"[smoke] 2 islands x {args.island_size} players, game={args.game}, ver={version}")

    if args.keep_generate and all(F.newest_multidata(i.out_dir) for i in islands):
        for i in islands:
            i.multidata = F.newest_multidata(i.out_dir)
        print("[smoke] reusing existing island multidata")
    else:
        F.generate_islands(islands, args.python, spoiler=0, dry=False)
    F.launch_servers(islands, args.python, args.base_port, dry=False)

    ok = False
    try:
        ok = asyncio.run(run_test(islands, version, args.game))
    finally:
        F.shutdown(islands)
        print("[smoke] islands stopped")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
