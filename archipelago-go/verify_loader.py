#!/usr/bin/env python3
"""
verify_loader.py — end-to-end acceptance test for the peliarch real-multidata loader
(specs/SPEC_remaining_go_functions.md Batch A).

Boots nothing itself: point it at a running `peliarch --multidata <bundle>` and the
same bundle JSON, and it drives a few real websocket clients to assert correct behavior:

  1. Connect handshake: RoomInfo -> Connect -> Connected with real missing_locations.
  2. Cross-slot routing: a finder checks a location; the CORRECT target receives the
     CORRECT item, source-tagged (player=finder, location=loc), with a monotonic index.
  3. LocationScouts returns the real item at a location.
  4. Auth gates: bad name -> InvalidSlot, wrong game -> InvalidGame, old ver -> IncompatibleVersion.

Exit code 0 = all assertions passed.
"""
import argparse
import asyncio
import json
import sys

import websockets


async def recv_until(ws, cmd, timeout=5.0):
    """Read frames until a command of type `cmd` arrives; return it."""
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout)
        for m in json.loads(raw):
            if m.get("cmd") == cmd:
                return m


async def send(ws, **kw):
    await ws.send(json.dumps([kw]))


async def connect_slot(uri, name, game, version=(0, 5, 1), tags=("Loadtest",)):
    ws = await websockets.connect(uri, max_size=None, ping_interval=None)
    await recv_until(ws, "RoomInfo")
    await send(ws, cmd="Connect", password=None, game=game, name=name,
               uuid="verify-" + name,
               version={"major": version[0], "minor": version[1], "build": version[2], "class": "Version"},
               items_handling=0, tags=list(tags), slot_data=False)
    return ws


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default="ws://127.0.0.1:38291")
    ap.add_argument("--bundle", required=True)
    args = ap.parse_args()

    bundle = json.load(open(args.bundle))
    results = []

    def check(label, cond):
        results.append((label, cond))
        print(("PASS" if cond else "FAIL"), label)

    # pick a finder slot with a location that routes to a DIFFERENT slot
    finder_slot = None
    finder_name = None
    loc = None
    target_slot = None
    expected_item = None
    expected_flags = None
    for name, (team, slot) in bundle["connect_names"].items():
        table = bundle["locations"].get(str(slot), {})
        for loc_id, (item, tgt, flags) in table.items():
            if tgt != slot:  # cross-slot
                finder_slot, finder_name, loc = slot, name, int(loc_id)
                target_slot, expected_item, expected_flags = tgt, item, flags
                break
        if loc is not None:
            break
    assert loc is not None, "no cross-slot location found in bundle"

    # name of the target slot
    target_name = next(n for n, (t, s) in bundle["connect_names"].items() if s == target_slot)
    finder_game = bundle["slot_info"][str(finder_slot)]["game"]
    target_game = bundle["slot_info"][str(target_slot)]["game"]
    print(f"scenario: {finder_name}(slot {finder_slot}) checks loc {loc} "
          f"-> item {expected_item} to {target_name}(slot {target_slot}) flags {expected_flags}")

    # ---- 1. handshake ----
    finder = await connect_slot(args.uri, finder_name, finder_game)
    connected = await recv_until(finder, "Connected")
    check("Connected.slot matches", connected.get("slot") == finder_slot)
    check("missing_locations includes the test loc", loc in connected.get("missing_locations", []))
    check("missing_locations is the real table size",
          len(connected.get("missing_locations", [])) == len(bundle["locations"][str(finder_slot)]))
    check("slot_info present for finder",
          str(finder_slot) in connected.get("slot_info", {}))

    # ---- 2. cross-slot routing ----
    target = await connect_slot(args.uri, target_name, target_game)
    await recv_until(target, "Connected")
    # finder checks the location
    await send(finder, cmd="LocationChecks", locations=[loc])
    ri = await recv_until(target, "ReceivedItems")
    items = ri.get("items", [])
    check("target received exactly one item", len(items) == 1)
    if items:
        it = items[0]
        check("received item id correct", it.get("item") == expected_item)
        check("received location correct", it.get("location") == loc)
        check("received source player = finder slot", it.get("player") == finder_slot)
        check("received flags correct", it.get("flags") == expected_flags)
        check("ReceivedItems index is monotonic int", isinstance(ri.get("index"), int) and ri.get("index") >= 0)

    # finder should also get a RoomUpdate reflecting the checked location
    ru = await recv_until(finder, "RoomUpdate")
    check("finder RoomUpdate lists the checked loc", loc in ru.get("checked_locations", []))

    # ---- 3. LocationScouts ----
    # pick another unchecked location for the finder
    scout_loc = next((int(l) for l in bundle["locations"][str(finder_slot)] if int(l) != loc), None)
    if scout_loc is not None:
        s_item, s_tgt, s_flags = bundle["locations"][str(finder_slot)][str(scout_loc)]
        await send(finder, cmd="LocationScouts", locations=[scout_loc], create_as_hint=0)
        li = await recv_until(finder, "LocationInfo")
        linfo = li.get("locations", [])
        ok = any(x.get("location") == scout_loc and x.get("item") == s_item for x in linfo)
        check("LocationScouts returns real item at location", ok)

    await finder.close()
    await target.close()

    # ---- 4. auth gates ----
    async def expect_refused(name, game, version, label, expected_err):
        ws = await websockets.connect(args.uri, max_size=None, ping_interval=None)
        await recv_until(ws, "RoomInfo")
        await send(ws, cmd="Connect", password=None, game=game, name=name, uuid="x",
                   version={"major": version[0], "minor": version[1], "build": version[2], "class": "Version"},
                   items_handling=0, tags=[], slot_data=False)
        m = await recv_until(ws, "ConnectionRefused")
        check(label, expected_err in m.get("errors", []))
        await ws.close()

    await expect_refused("NoSuchPlayer", finder_game, (0, 5, 1), "bad name -> InvalidSlot", "InvalidSlot")
    await expect_refused(finder_name, "WrongGame", (0, 5, 1), "wrong game -> InvalidGame", "InvalidGame")
    await expect_refused(finder_name, finder_game, (0, 1, 5), "old version -> IncompatibleVersion", "IncompatibleVersion")

    npass = sum(1 for _, c in results if c)
    print(f"\n{npass}/{len(results)} checks passed")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
