#!/usr/bin/env python3
"""Tail-batch checks (fast-fail): GetDataPackage, name-group seeding, CreateHints, admin."""
import argparse, asyncio, json, sys
import websockets

results = []
def check(label, cond):
    results.append((label, bool(cond))); print(("PASS" if cond else "FAIL"), label, flush=True)

async def recv_until(ws, cmd, timeout=2.0):
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout)
        for m in json.loads(raw):
            if m.get("cmd") == cmd:
                return m

async def maybe(ws, cmd, timeout=1.2):
    try:
        return await recv_until(ws, cmd, timeout)
    except asyncio.TimeoutError:
        return None

async def send(ws, **kw): await ws.send(json.dumps([kw]))

async def connect(uri, name, game, tags=("Loadtest",)):
    ws = await websockets.connect(uri, max_size=None, ping_interval=None)
    await recv_until(ws, "RoomInfo")
    await send(ws, cmd="Connect", password=None, game=game, name=name, uuid="c-"+name,
               version={"major":0,"minor":5,"build":1,"class":"Version"},
               items_handling=0, tags=list(tags), slot_data=False)
    await recv_until(ws, "Connected")
    return ws

async def run(uri, bundle):
    b = json.load(open(bundle))
    game = next(iter(b["datapackage_checksums"]))
    f_name = next(n for n,(t,s) in b["connect_names"].items() if s==1)
    table1 = b["locations"]["1"]; loc = int(next(iter(table1)))
    item, tgt, flags = table1[str(loc)]
    t_name = next(n for n,(t,s) in b["connect_names"].items() if s==tgt)
    tgame = b["slot_info"][str(tgt)]["game"]

    f = await connect(uri, f_name, b["slot_info"]["1"]["game"])

    await send(f, cmd="GetDataPackage", games=[game])
    dp = await maybe(f, "DataPackage")
    games = (dp or {}).get("data", {}).get("games", {})
    check("GetDataPackage returns the requested game", game in games)
    check("DataPackage has item_name_to_id", "item_name_to_id" in (games.get(game) or {}))

    k_ing = f"_read_item_name_groups_{game}"
    await send(f, cmd="Get", keys=[k_ing])
    rg = await maybe(f, "Retrieved")
    check("_read_item_name_groups_<game> seeded (non-null)",
          (rg or {}).get("keys", {}).get(k_ing) is not None)

    hint_key = "_read_hints_0_1"
    await send(f, cmd="SetNotify", keys=[hint_key])
    await send(f, cmd="CreateHints", locations=[loc])
    await asyncio.sleep(0.2)
    await send(f, cmd="Get", keys=[hint_key])
    rh = await maybe(f, "Retrieved")
    hints = (rh or {}).get("keys", {}).get(hint_key) or []
    found = any(h.get("location")==loc and h.get("item")==item and
                h.get("finding_player")==1 and h.get("receiving_player")==tgt for h in hints)
    check("CreateHints stored a correct hint in finder's _read_hints", found)

    await send(f, cmd="Say", text="!remaining")
    got = False
    for _ in range(3):
        pj = await maybe(f, "PrintJSON", 1.2)
        if pj is None: break
        if pj.get("type") == "CommandResult": got = True; break
    check("admin !remaining replies with a CommandResult", got)

    t = await connect(uri, t_name, tgame)
    await send(f, cmd="Say", text="!release")
    ri = await maybe(t, "ReceivedItems", 2.0)
    check("admin !release delivers the finder's items to a target",
          ri is not None and len(ri.get("items", [])) >= 1)

    for ws in (f, t):
        await ws.close()

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--uri", default="ws://127.0.0.1:38291")
    ap.add_argument("--bundle", required=True); a = ap.parse_args()
    asyncio.run(run(a.uri, a.bundle))
    n = sum(1 for _,c in results if c)
    print(f"\n{n}/{len(results)} checks passed", flush=True)

if __name__ == "__main__":
    main()
