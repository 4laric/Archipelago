# Stress test — run plan (tailored to this checkout)

This checkout is **Archipelago 0.6.6** (`Utils.__version__`), git `0.6.5-224-g093868ff`.
Everything below assumes you run from the Archipelago root (where `MultiServer.py` lives).

## TL;DR — one command

```bash
python run_loadtest.py --rungs 100,250,500,1000 --soak-seconds 180 --check-rate 1
```

`run_loadtest.py` does the whole flow: writes YAMLs, runs `Generate.py` once at the
largest rung, then for each rung starts a **fresh** `MultiServer`, drives the swarm
with `ap_loadtest.py`, samples server CPU/RSS, kills the server, and finally prints the
`sweep_compare` decision table. Results land in `loadtest_results/`.

Auto-detected for you (override if needed):
- `--version 0,6,6` — matched to this checkout, so no `ConnectionRefused: InvalidVersion`.
- `--game ChecksFinder` — **Clique is not installed here**; ChecksFinder is the ROM-free
  default that still routes items cross-slot, so `routing_latency_ms` is a real number.

## Recommended order

1. **Smoke first (~2 min)** — prove generation + hosting + the swarm all work on your box
   before committing to a long soak:
   ```bash
   python run_loadtest.py --rungs 50,100 --soak-seconds 30 --check-rate 1
   ```
2. **The real sweep** — once the smoke is clean:
   ```bash
   python run_loadtest.py --rungs 100,250,500,1000 --soak-seconds 180 --check-rate 1
   ```
3. **Push the rate** if nothing bends — `sweep_compare`'s "Read" line will literally tell
   you to. Re-run with `--check-rate 2` (or 3) until a metric knees.

`--dry-run` prints every command without executing — good for a sanity check.

## What you're looking for (the breakpoint signals)

- **probe RTT p95/max climbing, esp. periodic spikes** → single event-loop stall; the
  usual culprit is the synchronous `save()` (stop-the-world). Cheapest fix, no rewrite.
- **`checkLag` (checks_sent − items_recv) growing with slots** → routing throughput
  ceiling. This is the signal that justifies a bigger architectural change.
- **`fanoutP99 >> routeP95`** → datastorage `SetNotify` fan-out is the bottleneck (often
  the real killer with many trackers); fix = relay/shard/debounce the notify path.
- **`reconP95` + `backlog` large** → reconnect-storm cost from full `ReceivedItems`
  resend; fix = incremental resync.
- **`rssMB` rising monotonically over a long soak** → memory creep worth chasing.

The knee in those curves vs slot count is the number you take into the
ops-tuning-vs-rewrite decision.

## Manual flow (if you'd rather drive it yourself)

```bash
python gen_yamls.py --count 1000 --game ChecksFinder --out ./loadtest_players
python Generate.py --player_files_path ./loadtest_players --outputpath ./loadtest_out
python MultiServer.py ./loadtest_out/<the .archipelago or .zip> --port 38281 &
python ap_loadtest.py --host localhost --port 38281 --slots 1000 \
  --game ChecksFinder --version 0,6,6 --check-rate 1 --soak-seconds 180 \
  --server-pid <MultiServer PID> --out results_1000.json
# repeat the harness line per slot count against a freshly-restarted server, then:
python sweep_compare.py loadtest_results/results_*.json
```

## Notes / gotchas

- **Generation can be the slow part at 1000 slots.** ChecksFinder is light, but if
  `Generate.py` is slow or OOMs, drop the top rung or generate at a smaller max — the
  harness measures the *server*, not generation.
- **One box, ~1000 sockets is fine** (IO-bound). If the *load generator* CPU-saturates at
  high check rates, split clients across machines so you're measuring the server.
- **Local hosting is plain `ws://`** (what the harness uses). `archipelago.gg`-style TLS
  hosting would need `wss://` — not relevant for this local sweep.
- The harness reserves the **last** slot (`P1000`) for the loop-health probe.
