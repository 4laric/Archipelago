# Harness validation (done before any real run)

The harness was validated end-to-end against the included `mock_server.py` — a fake
server that implements only the protocol slice the harness exercises and can inject a
real blocking stall to simulate the synchronous `save()` stop-the-world. This proves
the *measurement* is sound before you point it at the real MultiServer.

## 1. End-to-end smoke (no stall)
40 slots, all four phases ran cleanly:

| metric | result |
|---|---|
| connect ramp | 39/39 driver slots + probe connected, 0 refused |
| routing latency | n=684 samples, p95 ≈ 2.1 ms (cross-slot pairing works) |
| fanout latency | n=760 samples, p95 ≈ 1.1 ms |
| reconnect | n=9, p95 ≈ 7.6 ms |
| throughput | 702 checks → 684 items |
| errors / refused | 0 / 0 |

## 2. Stall injection → probe spikes in lockstep (the key proof)
Mock injected a **400 ms** blocking stall every 1 s. The loop-health probe caught it:

| | p50 | p95 | p99 | max |
|---|---|---|---|---|
| baseline (no stall) | 0.58 ms | 0.66 | 0.74 | 0.77 |
| **400 ms stall injected** | 0.63 ms | **397** | **398** | **398** |

Median stays sub-millisecond (loop healthy between stalls) while the tail jumps to
~400 ms — exactly the injected stall. This is the signature you'll watch for on the
real server: a flat median with periodic tail spikes = a synchronous save stalling the
single event loop.

## 3. Sweep aggregation
A 2-rung mock sweep fed through `sweep_compare.py` produced the full decision table
(metric-vs-slots, growth multipliers, and the subsystem "Read" heuristic) — so the
final reporting stage works too.

## Environment
Validated with `websockets 16.0`, `psutil 7.2.2`, Python 3. The runner auto-detects
this checkout as **AP 0.6.6** and pins `--game ChecksFinder` (Clique isn't installed).
