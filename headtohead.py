#!/usr/bin/env python3
"""
headtohead.py - identical harness run at a fixed slot count against BOTH the Python
MultiServer and peliarch, back-to-back, printed side-by-side. One log format,
one set of harness params, same sampling - the controlled "sure sure" comparison.

Confound control: peliarch is launched with --locs-per-slot matched to the real
room's per-slot location count (ChecksFinder=25) so both servers do the same per-slot
work. The Python side reuses the existing multidata in loadtest_out (drives a subset).

  python headtohead.py --slots 250 --soak-seconds 180
  python headtohead.py --slots 250 --locs-per-slot 25 --go-bin ./archipelago-go/peliarch
"""
import argparse, json, os, subprocess, sys, time
from run_loadtest import (free_port, resolve_server_pid, stop_server,
                          wait_for_port, newest_multidata, detect_version)

HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[h2h] {msg}", flush=True)


def find_go_bin():
    for p in ("archipelago-go/peliarch.exe", "archipelago-go/peliarch"):
        f = os.path.join(HERE, p)
        if os.path.isfile(f):
            return f
    return None


def drive(py, port, server_pid, args, out):
    cmd = [py, os.path.join(HERE, "ap_loadtest.py"),
           "--host", "localhost", "--port", str(port),
           "--slots", str(args.slots), "--slot-prefix", "P", "--slot-digits", "4",
           "--game", args.game, "--version", args.version,
           "--ramp-seconds", str(args.ramp_seconds),
           "--soak-seconds", str(args.soak_seconds),
           "--check-rate", str(args.check_rate),
           "--tracker-fraction", str(args.tracker_fraction),
           "--reconnect-fraction", str(args.reconnect_fraction),
           "--out", out]
    if server_pid is not None:
        cmd += ["--server-pid", str(server_pid)]
    log("$ " + " ".join(str(c) for c in cmd))
    subprocess.call(cmd, cwd=HERE)


def run_phase(label, server_cmd, port, args, out):
    log(f"--- {label}: starting server on :{port} ---")
    log("$ " + " ".join(str(c) for c in server_cmd))
    srv = subprocess.Popen(server_cmd, cwd=HERE)
    if not wait_for_port("127.0.0.1", port, timeout=90):
        stop_server(srv); sys.exit(f"{label}: server didn't open port {port}")
    time.sleep(2)
    pid = resolve_server_pid(srv.pid, port)
    log(f"{label}: sampling server pid {pid}")
    drive(args.python, port, pid, args, out)
    stop_server(srv)
    log(f"--- {label}: done ---")


def summarize(path):
    d = json.load(open(path)); s = d["summary"]; ts = d.get("timeseries", [])
    cpu = [r[4] for r in ts if len(r) >= 5 and r[4] is not None]
    rss = [r[3] for r in ts if len(r) >= 5 and r[3] is not None]
    g = lambda a, b: (s.get(a, {}) or {}).get(b)
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
        "cpu med": (sorted(cpu)[len(cpu)//2] if cpu else None),
        "cpu max": (max(cpu) if cpu else None),
        "rss max": (max(rss) if rss else None),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slots", type=int, default=250)
    ap.add_argument("--locs-per-slot", type=int, default=25,
                    help="match the Python room's per-slot location count (ChecksFinder=25)")
    ap.add_argument("--go-bin", default=None)
    ap.add_argument("--multidata", default=None, help="Python room (default: newest in loadtest_out)")
    ap.add_argument("--out-dir", default=os.path.join(HERE, "loadtest_out"))
    ap.add_argument("--check-rate", type=float, default=1.0)
    ap.add_argument("--soak-seconds", type=float, default=180.0)
    ap.add_argument("--ramp-seconds", type=float, default=30.0)
    ap.add_argument("--tracker-fraction", type=float, default=0.3)
    ap.add_argument("--reconnect-fraction", type=float, default=0.25)
    ap.add_argument("--game", default="ChecksFinder")
    ap.add_argument("--version", default=None)
    ap.add_argument("--results-dir", default=os.path.join(HERE, "h2h_results"))
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--skip-python", action="store_true")
    ap.add_argument("--skip-go", action="store_true")
    args = ap.parse_args()
    args.version = args.version or detect_version()

    go_bin = args.go_bin or find_go_bin()
    multidata = args.multidata or newest_multidata(args.out_dir)
    run_id = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    os.makedirs(args.results_dir, exist_ok=True)
    py_out = os.path.join(args.results_dir, f"py_{run_id}.json")
    go_out = os.path.join(args.results_dir, f"go_{run_id}.json")

    log(f"head-to-head @ {args.slots} slots | check-rate {args.check_rate} | "
        f"soak {args.soak_seconds}s | locs/slot {args.locs_per_slot}")
    log(f"identical harness both sides; Python save ON (real), Go has no save")

    if not args.skip_python:
        if not multidata:
            sys.exit("no Python multidata; generate one (run_loadtest.py) or pass --multidata")
        for sv in (__import__("glob").glob(multidata + "_*") +
                   __import__("glob").glob(os.path.join(args.out_dir, "*.apsave"))):
            try: os.remove(sv)
            except OSError: pass
        pport = free_port(38281)
        run_phase("PYTHON MultiServer",
                  [args.python, os.path.join(HERE, "MultiServer.py"), multidata,
                   "--port", str(pport), "--host", "0.0.0.0"],
                  pport, args, py_out)

    if not args.skip_go:
        if not go_bin or not os.path.isfile(go_bin):
            sys.exit("peliarch binary not found; build it or pass --go-bin")
        gport = free_port(38291)
        run_phase("peliarch",
                  [go_bin, "--host", "0.0.0.0", "--port", str(gport),
                   "--locs-per-slot", str(args.locs_per_slot)],
                  gport, args, go_out)

    # side-by-side
    print("\n================ HEAD-TO-HEAD @ %d slots ================" % args.slots)
    py = summarize(py_out) if (not args.skip_python and os.path.isfile(py_out)) else None
    go = summarize(go_out) if (not args.skip_go and os.path.isfile(go_out)) else None
    keys = ["probe p50", "probe p95", "probe p99", "route p50", "route p95",
            "fanout p99", "checks", "items", "errors", "cpu med", "cpu max", "rss max"]
    w = 14
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


if __name__ == "__main__":
    main()
