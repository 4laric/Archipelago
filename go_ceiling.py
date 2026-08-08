#!/usr/bin/env python3
"""
go_ceiling.py — find peliarch's ceiling by FANNING many generators at one server.

One harness is a single asyncio process and tops out before a multi-core Go server does,
so this launches K parallel ap_loadtest.py processes against the SAME Go port and ramps
total load (K x slots-each) rung by rung. Because peliarch is synthetic (it assigns a
fresh slot per Connect), the generators need no coordination — the server can't tell them
apart, so total connections = generators x slots-per-gen.

Run this ON a generator box (or your laptop) pointed at the Go server's box. Watch the
SERVER's CPU separately with sample_server.py on its box — Go can exceed 100% (multi-core);
the ceiling is when it pegs all cores OR the aggregate latency climbs / errors appear.

  # server box:   ./peliarch --host 0.0.0.0 --port 38291
  #               python3 sample_server.py --port 38291 --out go_cpu.csv
  # generator box:
  python go_ceiling.py --host <SERVER_IP> --port 38291 \
      --rungs 1000,2000,4000,8000 --generators 16 --soak-seconds 120

Read the box's go_cpu.csv alongside this table: the rung where server CPU stops scaling
(all cores pegged) or aggregate probe/routing climbs is peliarch's per-box ceiling.
"""
import argparse, json, math, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[ceiling] {msg}", flush=True)


def pctmax(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return max(vals) if vals else None


def aggregate(paths):
    """Combine K per-generator result files for one rung.

    Throughput sums; latency we take the WORST percentile across generators (a clean
    server keeps every generator fast, so the max is the honest 'did anyone stall').
    Routing latency undercounts when fanned (cross-generator item routes don't match a
    sender's coord), so lean on probe + throughput + errors + the box CPU for the verdict.
    """
    checks = items = errors = live = 0
    probe95 = probe99 = route95 = []
    p95s, p99s, r95s = [], [], []
    for p in paths:
        if not os.path.isfile(p):
            continue
        s = json.load(open(p))["summary"]
        checks += s["throughput"]["checks_sent"]
        items += s["throughput"]["items_recv"]
        errors += s.get("errors", 0) or 0
        live += s["config"].get("live", 0) or 0
        p95s.append((s.get("loop_health_probe_rtt_ms", {}) or {}).get("p95"))
        p99s.append((s.get("loop_health_probe_rtt_ms", {}) or {}).get("p99"))
        r95s.append((s.get("routing_latency_ms", {}) or {}).get("p95"))
    return {"live": live, "checks": checks, "items": items, "errors": errors,
            "probe p95 (worst)": pctmax(p95s), "probe p99 (worst)": pctmax(p99s),
            "route p95 (worst)": pctmax(r95s)}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="peliarch server host/IP")
    ap.add_argument("--port", type=int, default=38291)
    ap.add_argument("--rungs", default="1000,2000,4000,8000",
                    help="comma-separated TOTAL connection counts to ramp")
    ap.add_argument("--generators", type=int, default=max(1, (os.cpu_count() or 4)),
                    help="parallel harness processes (default: this box's cores)")
    ap.add_argument("--check-rate", type=float, default=1.0)
    ap.add_argument("--soak-seconds", type=float, default=120.0)
    ap.add_argument("--ramp-seconds", type=float, default=30.0)
    ap.add_argument("--tracker-fraction", type=float, default=0.3)
    ap.add_argument("--reconnect-fraction", type=float, default=0.0,
                    help="0 by default — pure load ramp; raise to add reconnect storms")
    ap.add_argument("--game", default="ChecksFinder")
    ap.add_argument("--version", default="0,6,6")
    ap.add_argument("--results-dir", default=os.path.join(HERE, "go_ceiling_results"))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rungs = sorted(int(x) for x in args.rungs.split(","))
    K = args.generators
    run_id = args.run_id or time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    os.makedirs(args.results_dir, exist_ok=True)
    log(f"run_id={run_id}  server={args.host}:{args.port}  generators={K}  rungs={rungs}")
    log(f"watch the SERVER box: python3 sample_server.py --port {args.port} --out go_cpu.csv")

    table = []
    for total in rungs:
        per = math.ceil(total / K)
        actual = per * K
        log(f"=== rung total≈{actual} ({K} generators x {per} slots) ===")
        procs, outs = [], []
        for g in range(K):
            out = os.path.join(args.results_dir, f"r{total}_{run_id}_g{g:02d}.json")
            outs.append(out)
            cmd = [args.python, os.path.join(HERE, "ap_loadtest.py"),
                   "--host", args.host, "--port", str(args.port),
                   "--slots", str(per), "--slot-prefix", "P", "--slot-digits", "5",
                   "--game", args.game, "--version", args.version,
                   "--ramp-seconds", str(args.ramp_seconds),
                   "--soak-seconds", str(args.soak_seconds),
                   "--check-rate", str(args.check_rate),
                   "--tracker-fraction", str(args.tracker_fraction),
                   "--reconnect-fraction", str(args.reconnect_fraction),
                   "--out", out]
            if args.dry_run:
                if g == 0:
                    log("$ " + " ".join(str(c) for c in cmd) + f"   (x{K} generators)")
                continue
            lf = open(out + ".log", "w")
            procs.append(subprocess.Popen(cmd, cwd=HERE, stdout=lf, stderr=subprocess.STDOUT))
        if args.dry_run:
            continue
        for p in procs:
            p.wait()
        agg = aggregate(outs)
        agg["target"] = actual
        table.append(agg)
        log(f"rung {actual}: live={agg['live']} checks={agg['checks']} items={agg['items']} "
            f"errors={agg['errors']} probeP95worst={agg['probe p95 (worst)']} "
            f"routeP95worst={agg['route p95 (worst)']}")

    if args.dry_run:
        return
    cols = ["target", "live", "checks", "items", "errors",
            "probe p95 (worst)", "probe p99 (worst)", "route p95 (worst)"]
    w = 18
    print("\n===== peliarch CEILING RAMP (%d generators) =====" % K)
    print("".join(c.rjust(w) for c in cols))
    for row in table:
        print("".join(("-" if row.get(c) is None else str(row.get(c))).rjust(w) for c in cols))
    print("\nThe ceiling = the rung where the SERVER box's CPU stops scaling (all cores "
          "pegged, from go_cpu.csv) OR worst-probe/route climbs / errors appear. If nothing "
          "bends, you're generator-bound — add generator boxes or raise --check-rate.")
    print(f"per-generator results in {args.results_dir} (run_id={run_id})")


if __name__ == "__main__":
    main()
