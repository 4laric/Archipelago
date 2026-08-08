#!/usr/bin/env python3
"""
sample_server.py - log the MultiServer's CPU%% and RSS to a CSV, ON THE SERVER BOX.

When the load generator runs on a separate machine, ap_loadtest.py's --server-pid
sampling can't reach the server, so run THIS next to the server instead. It resolves
the process actually LISTENing on the port (so it samples the real worker, not a thin
launcher parent) and writes time,cpu_percent,rss_mb every interval.

Usage (on the server box, while the sweep runs from the other machine):
  python sample_server.py --port 38281 --interval 0.5 --out server_1000.csv
Stop with Ctrl+C when the rung finishes; combine the CSVs with the harness's
latency JSON to attribute server-bound vs generator-bound.
"""
import argparse, csv, sys, time

try:
    import psutil
except ImportError:
    sys.exit("pip install psutil")


def resolve_pid(port, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            for c in psutil.net_connections(kind="inet"):
                if (c.laddr and c.laddr.port == port and c.pid
                        and c.status == psutil.CONN_LISTEN):
                    return c.pid
        except Exception:
            pass
        time.sleep(0.5)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=38281, help="port the server listens on")
    ap.add_argument("--pid", type=int, default=None, help="sample this PID directly instead")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--out", default="server_sample.csv")
    args = ap.parse_args()

    pid = args.pid or resolve_pid(args.port)
    if not pid:
        sys.exit(f"no process LISTENing on port {args.port} (start the server first)")
    proc = psutil.Process(pid)
    print(f"sampling pid={pid} ({proc.name()}) every {args.interval}s -> {args.out}  [Ctrl+C to stop]")
    proc.cpu_percent(None)  # prime

    t0 = time.time()
    peak_cpu = peak_rss = 0.0
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "cpu_percent", "rss_mb"])
        try:
            while True:
                cpu = proc.cpu_percent(None)            # %% of ONE core; >100 = multi-core
                rss = proc.memory_info().rss / (1024 * 1024)
                peak_cpu = max(peak_cpu, cpu); peak_rss = max(peak_rss, rss)
                w.writerow([round(time.time() - t0, 2), round(cpu, 1), round(rss, 1)])
                f.flush()
                time.sleep(args.interval)
        except (KeyboardInterrupt, psutil.NoSuchProcess):
            pass
    print(f"\nwrote {args.out}.  peak CPU {peak_cpu:.0f}%  peak RSS {peak_rss:.0f} MB")
    print("(single-threaded server: CPU ~100% = one core saturated)")


if __name__ == "__main__":
    main()
