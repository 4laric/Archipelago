#!/usr/bin/env python3
"""
Self-contained integration check: spawns peliarch, runs loader + new-command
assertions (DeathLink Bounce, Set operations w/ original_value, Sync), tears down.
One process so the sandbox shell only waits on a single child.

Usage: python run_checks.py --bin /tmp/build/peliarch --bundle checksfinder.apgo.json
"""
import argparse, asyncio, json, socket, subprocess, sys, time
import websockets

results = []
def check(label, cond):
    results.append((label, bool(cond)))
    print(("PASS" if cond else "FAIL"), label)

async def recv_until(ws, cmd, timeout=5.0):
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout)
        for m in json.loads(raw):
            if m.get("cmd") == cmd:
                return m

async def send(ws, **kw):
    await ws.send(json.dumps([kw]))

async def connect(uri, name, game, version=(0,5,1), tags=("Loadtest",)):
    ws = await websockets.connect(uri, max_size=None, ping_interval=None)
    await recv_until(ws, "RoomInfo")
    await send(ws, cmd="Connect", password=None, game=game, name=name, uuid="c-"+name,
               version={"major":version[0],"minor":version[1],"build":version[2],"class":"Version"},
               items_handling=0, tags=list(tags), slot_data=False)
    return ws

def wait_port(host, port, secs=8):
    t0=time.time()
    while time.time()-t0 < secs:
        try:
            with socket.create_connection((host, port), 0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False

async def run(uri, bundle):
    b = json.load(open(bundle))
    # --- pick a cross-slot location ---
    finder=tgt=loc=item=flags=None; fname=None
    for name,(team,slot) in b["connect_names"].items():
        for lid,(it,tp,fl) in b["locations"].get(str(slot),{}).items():
            if tp != slot:
                finder, fname, loc, tgt, item, flags = slot, name, int(lid), tp, it, fl; break
        if loc is not None: break
    tname = next(n for n,(t,s) in b["connect_names"].items() if s==tgt)
    fgame = b["slot_info"][str(finder)]["game"]; tgame = b["slot_info"][str(tgt)]["game"]

    # --- loader: routing ---
    f = await connect(uri, fname, fgame); await recv_until(f, "Connected")
    t = await connect(uri, tname, tgame); await recv_until(t, "Connected")
    await send(f, cmd="LocationChecks", locations=[loc])
    ri = await recv_until(t, "ReceivedItems")
    it0 = ri["items"][0]
    check("routing: correct item to correct target",
          it0["item"]==item and it0["location"]==loc and it0["player"]==finder and it0["flags"]==flags)
    check("routing: item carries class for real clients", it0.get("class")=="NetworkItem")

    # --- Sync: resend full received list at index 0 ---
    await send(t, cmd="Sync")
    sy = await recv_until(t, "ReceivedItems")
    check("Sync: resends index 0 with the item", sy.get("index")==0 and any(x["location"]==loc for x in sy["items"]))

    # --- Datastore Set: add op + SetReply original_value ---
    key = "verifykey"
    await send(f, cmd="SetNotify", keys=[key])
    await send(f, cmd="Set", key=key, default=0, want_reply=True,
               operations=[{"operation":"add","value":5}])
    sr = await recv_until(f, "SetReply")
    check("Set add: 0 -> 5", sr.get("value")==5)
    check("Set add: original_value reported", sr.get("original_value")==0)
    await send(f, cmd="Set", key=key, default=0, want_reply=True,
               operations=[{"operation":"add","value":3}])
    sr2 = await recv_until(f, "SetReply")
    check("Set add: 5 -> 8 (accumulates)", sr2.get("value")==8 and sr2.get("original_value")==5)
    await send(f, cmd="Set", key=key, default=0, want_reply=True,
               operations=[{"operation":"max","value":4}])
    sr3 = await recv_until(f, "SetReply")
    check("Set max: max(8,4)=8", sr3.get("value")==8)

    # --- Bounce / DeathLink ---
    dl1 = await connect(uri, fname, fgame, tags=("DeathLink",))
    await recv_until(dl1, "Connected")
    # need a SECOND DeathLink client on a different slot
    dl2name = next(n for n,(tm,s) in b["connect_names"].items() if s not in (finder,))
    dl2slot = b["connect_names"][dl2name][1]
    dl2game = b["slot_info"][str(dl2slot)]["game"]
    dl2 = await connect(uri, dl2name, dl2game, tags=("DeathLink",))
    await recv_until(dl2, "Connected")
    payload = {"time": 123.0, "cause": "verify death", "source": fname}
    await send(dl1, cmd="Bounce", tags=["DeathLink"], data=payload)
    bnc = await recv_until(dl2, "Bounced")
    check("DeathLink: other tagged client receives Bounced", bnc.get("cmd")=="Bounced")
    check("DeathLink: payload forwarded verbatim", bnc.get("data")==payload)
    # a non-DeathLink client must NOT receive it
    plain = await connect(uri, tname if tname!=dl2name else fname, tgame, tags=())
    await recv_until(plain, "Connected")
    await send(dl1, cmd="Bounce", tags=["DeathLink"], data={"x":1})
    got_plain = False
    try:
        await recv_until(plain, "Bounced", timeout=1.0)
        got_plain = True
    except asyncio.TimeoutError:
        pass
    check("DeathLink: untagged client gets nothing", not got_plain)

    for ws in (f,t,dl1,dl2,plain):
        await ws.close()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--bin", required=True)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=38293)
    a=ap.parse_args()
    srv=subprocess.Popen([a.bin,"--host",a.host,"--port",str(a.port),"--multidata",a.bundle],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(a.host,a.port):
            print("server did not come up"); sys.exit(2)
        asyncio.run(run(f"ws://{a.host}:{a.port}", a.bundle))
    finally:
        srv.terminate()
        try: srv.wait(timeout=3)
        except subprocess.TimeoutExpired: srv.kill()
    npass=sum(1 for _,c in results if c)
    print(f"\n{npass}/{len(results)} checks passed")
    sys.exit(0 if npass==len(results) else 1)

if __name__=="__main__":
    main()
