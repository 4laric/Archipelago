#!/usr/bin/env python3
"""
headtohead_remote.py — run the head-to-head against a REMOTE host (e.g. your Hetzner box).

The server runs on the box; the load generator runs HERE on your laptop. That makes this
the clean, off-box measurement we wanted: the only thing between the harness and the server
is the real network, so the latencies are what players would actually see. Drives the two
remote ports SEQUENTIALLY (Python first, then Go), identical harness params, side-by-side.

Server CPU can't be sampled remotely (--server-pid is local-only), so run sample_server.py
ON the box during each phase and pass the CSVs with --py-cpu-csv / --go-cpu-csv to merge
peak CPU/RSS into the table. Optional.

  python headtohead_remote.py --host 203.0.113.10 --slots 250 --soak-seconds 180
  python headtohead_remote.py --host 203.0.113.10 --py-port 38281 --go-port 38291 \
         --slots 250 --py-cpu-csv py_cpu.csv --go-cpu-csv go_cpu.csv
"""
import argparse, csv, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[h2h-remote] {msg}", flush=True)


def drive(host, port, slots, args, out):
    cmd = [args.python, os.path.join(HERE, "ap_loadtest.py"),
           "--host", host, "--port", str(port),
           "--slots", str(slots), "--slot-prefix", "P", "--slot-digits", "4",
           "--game", args.game, "--version", args.version,
           "--ramp-seconds", str(args.ramp_seconds),
           "--soak-seconds", str(args.soak_seconds),
           "--check-rate", str(args.check_rate),
           "--tracker-fraction", str(args.tracker_fraction),
           "--reconnect-fraction", str(args.reconnect_fraction),
           "--out", out]
    # NOTE: no --server-pid — the server is remote; CPU comes from sample_server.py on the box.
    log("$ " + " ".join(str(c) for c in cmd))
    subprocess.call(cmd, cwd=HERE)


def cpu_from_csv(path):
    if not path or not os.path.isfile(path):
        return None, None
    cpu = rss = 0.0
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                cpu = max(cpu, float(row["cpu_percent"]))
                rss = max(rss, float(row["rss_mb"]))
            except (KeyError, ValueError):
                pass
    return round(cpu, 0), round(rss, 0)


def summarize(path, cpu_csv=None):
    d = json.load(open(path)); s = d["summary"]
    g = lambda a, b: (s.get(a, {}) or {}).get(b)
    cpu_max, rss_max = cpu_from_csv(cpu_csv)
    return {
        "probe p50": g("loop_health_probe_rtt_ms", "p50"),
        "probe p95": g("loop_health_probe_rtt_ms", "p95"),
        "probe p99": g("loop_health_probe_rtt_ms", "p99"),
        "route p50": g("routing_latency_ms", "p50"),
        "route p95": g("routing_latency_ms", "p95"),
        "fanout p99": g("fanout_latency_ms", "p99"),
        "checks": g("throughput", "checks_sent"),
        "items": g("throughput", "items_recv"),
        "errors": s.get("errors"),
        "cpu max (box)": cpu_max,
        "rss max (box)": rss_max,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="the Hetzner box's public IP / hostname")
    ap.add_argument("--py-port", type=int, default=38281)
    ap.add_argument("--go-port", type=int, default=38291)
    ap.add_argument("--slots", type=int, default=250)
    ap.add_argument("--check-rate", type=float, default=1.0)
    ap.add_argument("--soak-seconds", type=float, default=180.0)
    ap.add_argument("--ramp-seconds", type=float, default=30.0)
    ap.add_argument("--tracker-fraction", type=float, default=0.3)
    ap.add_argument("--reconnect-fraction", type=float, default=0.25)
    ap.add_argument("--game", default="ChecksFinder")
    ap.add_argument("--version", default="0,6,6")
    ap.add_argument("--results-dir", default=os.path.join(HERE, "h2h_remote_results"))
    ap.add_argument("--py-cpu-csv", default=None, help="sample_server.py CSV from the box (py phase)")
    ap.add_argument("--go-cpu-csv", default=None, help="sample_server.py CSV from the box (go phase)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--skip-python", action="store_true")
    ap.add_argument("--skip-go", action="store_true")
    args = ap.parse_args()

    run_id = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    os.makedirs(args.results_dir, exist_ok=True)
    py_out = os.path.join(args.results_dir, f"py_{run_id}.json")
    go_out = os.path.join(args.results_dir, f"go_{run_id}.json")

    log(f"remote host {args.host} | {args.slots} slots | soak {args.soak_seconds}s | "
        f"check-rate {args.check_rate}")
    log("server on the box, generator HERE — latencies include real network RTT")

    if not args.skip_python:
        log(f"=== PHASE 1: Python MultiServer @ {args.host}:{args.py_port} ===")
        drive(args.host, args.py_port, args.slots, args, py_out)
    if not args.skip_go:
        log(f"=== PHASE 2: peliarch @ {args.host}:{args.go_port} ===")
        drive(args.host, args.go_port, args.slots, args, go_out)

    print("\n========= REMOTE HEAD-TO-HEAD @ %d slots (host %s) =========" % (args.slots, args.host))
    py = summarize(py_out, args.py_cpu_csv) if (not args.skip_python and os.path.isfile(py_out)) else None
    go = summarize(go_out, args.go_cpu_csv) if (not args.skip_go and os.path.isfile(go_out)) else None
    keys = ["probe p50", "probe p95", "probe p99", "route p50", "route p95",
            "fanout p99", "checks", "items", "errors", "cpu max (box)", "rss max (box)"]
    w = 16
    print("metric".ljust(w) + "Python".rjust(w) + "peliarch".rjust(w) + "  ratio(py/go)")
    for k in keys:
        pv = py.get(k) if py else None
        gv = go.get(k) if go else None
        ratio = ""
        if isinstance(pv, (int, float)) and isinstance(gv, (int, float)) and gv:
            ratio = f"  {pv/gv:.0f}x" if pv >= gv else f"  {pv/gv:.2f}x"
        f = lambda v: "-" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))
        print(k.ljust(w) + f(pv).rjust(w) + f(gv).rjust(w) + ratio)
    print(f"\nresults: {py_out}  |  {go_out}")
    print("note: probe/route baselines now include your laptop↔box network RTT; the WALL "
          "behavior (Python collapse vs Go flat) is what to read, not the absolute floor.")


if __name__ == "__main__":
    main()
