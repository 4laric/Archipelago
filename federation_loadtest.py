#!/usr/bin/env python3
"""
federation_loadtest.py - stress test the whole islands federation at once.

Stands up K islands (+ the spectator bridge, so islands are measured under the real
relay load), then drives ALL islands concurrently: one ap_loadtest.py swarm per island
hitting its own port with n player slots, each sampling its own island's server. Then
aggregates a per-island verdict against the FINDINGS "healthy <=n" baseline.

The federation PASSES if every island independently looks like the standalone single-room
baseline (probe tail low, near-zero errors) while carrying its share of the population
and the cross-island bridge traffic. That's the "1000 across islands actually holds" proof.

  python federation_loadtest.py --total 1000 --island-size 150 --soak-seconds 180
  python federation_loadtest.py --islands 3 --island-size 100 --check-rate 1
  python federation_loadtest.py --islands 2 --island-size 30 --soak-seconds 30 --no-bridge
  python federation_loadtest.py --total 300 --island-size 100 --dry-run
"""
import argparse, asyncio, json, os, subprocess, sys, threading, time
import federation as F
from run_loadtest import detect_version

HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[fedload] {msg}", flush=True)


# ---- bridge running in a background thread (so islands are measured WITH relay load) ----

class BridgeThread(threading.Thread):
    def __init__(self, islands, version):
        super().__init__(daemon=True)
        self.islands = islands
        self.version = version
        self.stop = threading.Event()
        self.linked = threading.Event()

    def run(self):
        async def go():
            bridge = F.Bridge(self.islands, self.version)
            await asyncio.gather(*(bridge.connect_one(i) for i in self.islands))
            for isl in self.islands:
                try:
                    await asyncio.wait_for(bridge.ready[isl.idx].wait(), 30)
                except asyncio.TimeoutError:
                    pass
            self.linked.set()
            while not self.stop.is_set():
                await asyncio.sleep(0.5)
            for w in bridge.conns.values():
                try:
                    await w.close()
                except Exception:
                    pass
        try:
            asyncio.run(go())
        except Exception as e:
            log(f"bridge thread error: {type(e).__name__}: {e}")


# ---- aggregation ----

def load_summary(path):
    with open(path) as f:
        d = json.load(f)
    s = d["summary"]
    ts = d.get("timeseries", [])
    cpu = [r[4] for r in ts if len(r) >= 5 and r[4] is not None]
    rss = [r[3] for r in ts if len(r) >= 5 and r[3] is not None]
    g = lambda *k: (s.get(k[0], {}) or {}).get(k[1])
    return {
        "live": s["config"].get("live"),
        "probe_p95": g("loop_health_probe_rtt_ms", "p95"),
        "probe_p99": g("loop_health_probe_rtt_ms", "p99"),
        "route_p50": g("routing_latency_ms", "p50"),
        "route_p95": g("routing_latency_ms", "p95"),
        "checks": g("throughput", "checks_sent"),
        "items": g("throughput", "items_recv"),
        "errors": s.get("errors"),
        "refused": s.get("refused"),
        "cpu_max": round(max(cpu), 0) if cpu else None,
        "rss_max": round(max(rss), 0) if rss else None,
    }


def aggregate(islands, results, probe_budget_ms):
    rows = []
    for isl, path in zip(islands, results):
        try:
            r = load_summary(path)
        except Exception as e:
            log(f"island {isl.idx}: could not read results ({e})")
            r = None
        rows.append((isl, r))

    cols = ["isle", "live", "probeP95", "probeP99", "routeP50", "routeP95",
            "checks", "items", "err", "refd", "cpuMax", "rssMB", "verdict"]
    w = 9
    print("\n===== FEDERATION RESULTS (per island) =====")
    print("".join(c.rjust(w) for c in cols))
    healthy = degraded = 0
    for isl, r in rows:
        if r is None:
            print(f"I{isl.idx:02d}".rjust(w) + "  (no results)")
            degraded += 1
            continue
        ok = (r["errors"] in (0, None)) and (r["probe_p95"] is not None
                                             and r["probe_p95"] <= probe_budget_ms)
        verdict = "ok" if ok else "DEGRADED"
        healthy += ok
        degraded += (not ok)
        cells = [f"I{isl.idx:02d}", r["live"], r["probe_p95"], r["probe_p99"],
                 r["route_p50"], r["route_p95"], r["checks"], r["items"],
                 r["errors"], r["refused"], r["cpu_max"], r["rss_max"], verdict]
        print("".join(("-" if c is None else str(c)).rjust(w) for c in cells))

    print(f"\nbaseline budget: probe p95 <= {probe_budget_ms} ms and 0 errors per island")
    if degraded == 0:
        print(f"PASS: all {healthy} islands held within the healthy baseline under load + bridge.")
    else:
        print(f"FAIL: {degraded} island(s) degraded, {healthy} ok. "
              "Lower --island-size or check the bridge relay scope.")
    return degraded == 0


# ---- main ----

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--total", type=int, help="total players; islands = ceil(total/size)")
    g.add_argument("--islands", type=int, help="explicit island count")
    ap.add_argument("--island-size", type=int, default=150, help="player slots per island")
    ap.add_argument("--game", default="ChecksFinder")
    ap.add_argument("--version", default=None)
    ap.add_argument("--base-port", type=int, default=38300)
    ap.add_argument("--check-rate", type=float, default=1.0)
    ap.add_argument("--soak-seconds", type=float, default=180.0)
    ap.add_argument("--ramp-seconds", type=float, default=30.0)
    ap.add_argument("--tracker-fraction", type=float, default=0.3)
    ap.add_argument("--reconnect-fraction", type=float, default=0.25)
    ap.add_argument("--probe-budget-ms", type=float, default=500.0,
                    help="per-island pass threshold for probe p95 (FINDINGS healthy zone)")
    ap.add_argument("--spoiler", type=int, default=0)
    ap.add_argument("--results-dir", default=os.path.join(HERE, "federation_results"))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--no-bridge", action="store_true", help="measure islands WITHOUT the bridge")
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
    run_id = args.run_id or time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    os.makedirs(args.results_dir, exist_ok=True)
    islands = [F.Island(i, args.island_size, args.game) for i in range(k)]
    py = args.python
    log(f"run_id={run_id}  {k} islands x {args.island_size} players  ver={version}  "
        f"bridge={'off' if args.no_bridge else 'on'}")

    # 1. generate + 2. launch servers (reuse federation orchestration)
    if not args.skip_generate:
        F.generate_islands(islands, py, args.spoiler, args.dry_run)
    else:
        for isl in islands:
            isl.multidata = F.newest_multidata(isl.out_dir)
    F.launch_servers(islands, py, args.base_port, args.dry_run)

    if args.dry_run:
        for isl in islands:
            drive = harness_cmd(py, isl, args, run_id, None)
            log("$ " + " ".join(str(c) for c in drive))
        log("dry-run: would start bridge + run the above concurrently, then aggregate.")
        return

    # 3. bring up the bridge (so islands carry relay load while measured)
    bridge = None
    if not args.no_bridge:
        bridge = BridgeThread(islands, version)
        bridge.start()
        bridge.linked.wait(timeout=40)
        log("bridge linked" if bridge.linked.is_set() else "bridge link timed out (continuing)")

    # 4. drive every island concurrently
    results, procs, logs = [], [], []
    for isl in islands:
        out = os.path.join(args.results_dir, f"results_{run_id}_I{isl.idx:02d}.json")
        results.append(out)
        lf = open(os.path.join(args.results_dir, f"harness_{run_id}_I{isl.idx:02d}.log"), "w")
        logs.append(lf)
        cmd = harness_cmd(py, isl, args, run_id, out)
        log(f"island {isl.idx}: driving {args.island_size} slots on :{isl.port} "
            f"(server pid {isl.server_pid})")
        procs.append(subprocess.Popen(cmd, cwd=HERE, stdout=lf, stderr=subprocess.STDOUT))

    log(f"all {k} swarms running concurrently; waiting...")
    for p in procs:
        p.wait()
    for lf in logs:
        lf.close()

    # 5. teardown + aggregate
    if bridge:
        bridge.stop.set()
        bridge.join(timeout=5)
    F.shutdown(islands)
    ok = aggregate(islands, results, args.probe_budget_ms)
    log(f"done. per-island results + harness logs in {args.results_dir} (run_id={run_id})")
    sys.exit(0 if ok else 1)


def harness_cmd(py, isl, args, run_id, out):
    return [py, os.path.join(HERE, "ap_loadtest.py"),
            "--host", "localhost", "--port", str(isl.port),
            "--slots", str(isl.size), "--slot-prefix", isl.prefix, "--slot-digits", "4",
            "--game", isl.game, "--version", ",".join(str(x) for x in
                (tuple(int(v) for v in (args.version or detect_version()).split(",")))),
            "--ramp-seconds", str(args.ramp_seconds),
            "--soak-seconds", str(args.soak_seconds),
            "--check-rate", str(args.check_rate),
            "--tracker-fraction", str(args.tracker_fraction),
            "--reconnect-fraction", str(args.reconnect_fraction),
            "--out", out or "<results>"] + (
            ["--server-pid", str(isl.server_pid)] if isl.server_pid else [])


if __name__ == "__main__":
    main()
