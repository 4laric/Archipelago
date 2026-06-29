#!/usr/bin/env python3
"""
go_sweep.py - sweep archipela-go with the SAME methodology as run_loadtest.py, so the
Go curves are directly comparable to the Python MultiServer baseline in FINDINGS.md.

Per rung: start a fresh archipela-go on a free port, sample its CPU/RSS via --server-pid,
drive ap_loadtest.py at that slot count, kill it, next rung; then sweep_compare.
Synthetic (no multidata) - same load shape both sides = clean architecture comparison.

  python go_sweep.py --rungs 100,250,500,1000 --soak-seconds 180 --check-rate 1
  python go_sweep.py --go-bin ./archipelago-go/archipela-go --rungs 100,250 --dry-run
"""
import argparse, os, socket, subprocess, sys, time
from run_loadtest import free_port, resolve_server_pid, stop_server, wait_for_port

HERE = os.path.dirname(os.path.abspath(__file__))
_LOGF = None


def log(msg):
    line = f"[gosweep] {msg}"
    print(line, flush=True)
    if _LOGF:
        _LOGF.write(line + "\n"); _LOGF.flush()


def find_go_bin():
    for p in ("archipelago-go/archipela-go.exe", "archipelago-go/archipela-go",
              "archipela-go.exe", "archipela-go"):
        full = os.path.join(HERE, p)
        if os.path.isfile(full):
            return full
    return None


def main():
    global _LOGF
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--go-bin", default=None, help="path to the built archipela-go binary")
    ap.add_argument("--rungs", default="100,250,500,1000")
    ap.add_argument("--port", type=int, default=38281)
    ap.add_argument("--check-rate", type=float, default=1.0)
    ap.add_argument("--soak-seconds", type=float, default=180.0)
    ap.add_argument("--ramp-seconds", type=float, default=30.0)
    ap.add_argument("--tracker-fraction", type=float, default=0.3)
    ap.add_argument("--reconnect-fraction", type=float, default=0.25)
    ap.add_argument("--game", default="ChecksFinder")
    ap.add_argument("--version", default="0,6,1", help="synthetic server ignores it")
    ap.add_argument("--results-dir", default=os.path.join(HERE, "go_results"))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    go_bin = args.go_bin or find_go_bin()
    if not go_bin or (not args.dry_run and not os.path.isfile(go_bin)):
        sys.exit("archipela-go binary not found; build it (go build -o archipela-go .) "
                 "or pass --go-bin")
    rungs = sorted(int(x) for x in args.rungs.split(","))
    run_id = args.run_id or time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    os.makedirs(args.results_dir, exist_ok=True)
    if not args.dry_run:
        _LOGF = open(os.path.join(args.results_dir, f"run_{run_id}.log"), "w")
    py = args.python
    log(f"run_id={run_id}  go_bin={go_bin}  rungs={rungs}")

    results = []
    for n in rungs:
        port = free_port(args.port) if not args.dry_run else args.port
        log(f"=== rung {n} on port {port} ===")
        srv_cmd = [go_bin, "--host", "0.0.0.0", "--port", str(port)]
        log("$ " + " ".join(srv_cmd))
        srv = None
        server_pid = None
        if not args.dry_run:
            srv = subprocess.Popen(srv_cmd, cwd=HERE)
            if not wait_for_port("127.0.0.1", port, timeout=30):
                stop_server(srv); sys.exit(f"archipela-go didn't open port {port}")
            time.sleep(1)
            server_pid = resolve_server_pid(srv.pid, port)
            log(f"server pid={server_pid}")

        out = os.path.join(args.results_dir, f"results_{run_id}_{n}.json")
        drive = [py, os.path.join(HERE, "ap_loadtest.py"),
                 "--host", "localhost", "--port", str(port),
                 "--slots", str(n), "--slot-prefix", "P", "--slot-digits", "4",
                 "--game", args.game, "--version", args.version,
                 "--ramp-seconds", str(args.ramp_seconds),
                 "--soak-seconds", str(args.soak_seconds),
                 "--check-rate", str(args.check_rate),
                 "--tracker-fraction", str(args.tracker_fraction),
                 "--reconnect-fraction", str(args.reconnect_fraction),
                 "--out", out]
        if server_pid is not None:
            drive += ["--server-pid", str(server_pid)]
        log("$ " + " ".join(str(c) for c in drive))
        if not args.dry_run:
            subprocess.call(drive, cwd=HERE)
        results.append(out)
        stop_server(srv)

    log("=== sweep comparison (archipela-go) ===")
    cmp_cmd = [py, os.path.join(HERE, "sweep_compare.py")] + results
    log("$ " + " ".join(str(c) for c in cmp_cmd))
    if not args.dry_run:
        subprocess.call(cmp_cmd, cwd=HERE)
        log(f"done. results in {args.results_dir} (run_id={run_id}). "
            "Compare these curves to the Python baseline in FINDINGS.md.")
        if _LOGF:
            _LOGF.close()


if __name__ == "__main__":
    main()
