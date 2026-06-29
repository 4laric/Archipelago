#!/usr/bin/env python3
"""
sweep_compare.py — turn several ap_loadtest results files into one decision table.

Run the harness once per slot count (each writes its own results json), then:
  python sweep_compare.py results_100.json results_250.json results_500.json results_1000.json

It prints metric-vs-slots, the multiplier each metric grew vs the previous rung
(so a jump out of proportion to the slot increase = the knee), and a one-line read
on which subsystem bends first -> which product you're actually building.
"""
import argparse, json, sys


def load(path):
    with open(path) as f:
        d = json.load(f)
    s = d.get("summary", d)
    ts = d.get("timeseries", [])
    peak_rss = peak_cpu = None
    max_lag = 0
    for row in ts:
        # row: [t, checks_sent, items_recv, rss, cpu]
        if len(row) >= 5:
            _, cs, ir, rss, cpu = row[:5]
            if rss is not None:
                peak_rss = rss if peak_rss is None else max(peak_rss, rss)
            if cpu is not None:
                peak_cpu = cpu if peak_cpu is None else max(peak_cpu, cpu)
            max_lag = max(max_lag, (cs or 0) - (ir or 0))
    g = lambda d, *ks: (d or {}).get(ks[-1]) if len(ks) == 1 else g(d.get(ks[0], {}), *ks[1:])
    return {
        "slots": s["config"]["slots"],
        "live": s["config"].get("live"),
        "probe_p95": g(s, "loop_health_probe_rtt_ms", "p95"),
        "probe_p99": g(s, "loop_health_probe_rtt_ms", "p99"),
        "probe_max": g(s, "loop_health_probe_rtt_ms", "max"),
        "route_p95": g(s, "routing_latency_ms", "p95"),
        "route_p99": g(s, "routing_latency_ms", "p99"),
        "fanout_p99": g(s, "fanout_latency_ms", "p99"),
        "recon_p95": g(s, "reconnect", "time_to_connected_ms_p95"),
        "backlog_p95": g(s, "reconnect", "backlog_items_p95"),
        "checks": g(s, "throughput", "checks_sent"),
        "items": g(s, "throughput", "items_recv"),
        "max_lag": max_lag,
        "peak_rss_mb": round(peak_rss, 1) if peak_rss else None,
        "peak_cpu_pct": round(peak_cpu, 1) if peak_cpu else None,
        "errors": s.get("errors"),
        "refused": s.get("refused"),
    }


def fmt(v):
    return "-" if v is None else (f"{v:.0f}" if isinstance(v, float) else str(v))


def mult(cur, prev):
    if cur is None or prev in (None, 0):
        return "-"
    return f"{cur / prev:.1f}x"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()
    rows = sorted((load(f) for f in args.files), key=lambda r: r["slots"])

    cols = [("slots", "slots"), ("live", "live"),
            ("probe_p95", "probeP95"), ("probe_p99", "probeP99"), ("probe_max", "probeMax"),
            ("route_p95", "routeP95"), ("route_p99", "routeP99"),
            ("fanout_p99", "fanoutP99"), ("recon_p95", "reconP95"),
            ("backlog_p95", "backlog"), ("max_lag", "checkLag"),
            ("peak_rss_mb", "rssMB"), ("peak_cpu_pct", "cpu%"),
            ("errors", "err"), ("refused", "refd")]
    w = 10
    print("".join(h.rjust(w) for _, h in cols))
    for r in rows:
        print("".join(fmt(r[k]).rjust(w) for k, _ in cols))

    # growth multipliers vs previous rung for the diagnostic metrics
    print("\nGrowth vs previous rung (watch for jumps out of proportion to slot increase):")
    diag = [("probe_p95", "loop RTT p95"), ("route_p95", "routing p95"),
            ("fanout_p99", "fanout p99"), ("max_lag", "check->item lag"),
            ("peak_rss_mb", "peak RSS")]
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        slot_mult = cur["slots"] / prev["slots"] if prev["slots"] else 1
        parts = [f"{cur['slots']} vs {prev['slots']} (slots x{slot_mult:.1f}):"]
        for k, label in diag:
            parts.append(f"{label} {mult(cur[k], prev[k])}")
        print("  " + "  ".join(parts))

    # crude read on which subsystem bends first
    print("\nRead:")
    last = rows[-1]
    flags = []
    if last["probe_max"] and last["probe_p95"] and last["probe_max"] > 3 * (last["probe_p95"] or 1):
        flags.append("loop RTT max >> p95 -> periodic stall (likely synchronous save) is the first wall; "
                     "cheapest fix, probably async/incremental save, no rewrite")
    if last["max_lag"] and last["checks"] and last["max_lag"] > 0.1 * last["checks"]:
        flags.append("check->item lag is a big fraction of checks -> routing throughput ceiling; "
                     "this is the signal that justifies the Go + Python-sidecar server")
    if last["fanout_p99"] and last["route_p95"] and last["fanout_p99"] > 3 * (last["route_p95"] or 1):
        flags.append("fanout p99 >> routing p95 -> datastorage SetNotify is the bottleneck; "
                     "fix = relay/shard the notify path or debounce tracker writes, not a rewrite")
    if not flags:
        flags.append("nothing bends sharply at this rate -> push --check-rate up and re-sweep until something does")
    for f in flags:
        print("  - " + f)


if __name__ == "__main__":
    main()
