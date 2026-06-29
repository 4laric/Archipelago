# Archipelago MultiServer — single-room scaling findings

**Question:** how far can one stock `MultiServer` room scale toward ~1000 concurrent
slots (the streamer community-sync case), and is the path to 1000 an ops tune or a
rewrite?

**Setup:** AP 0.6.6 checkout, game ChecksFinder (ROM-free, routes items cross-slot),
1 check / client / sec, 180 s soak per rung, `tracker_fraction 0.3` unless noted.
Cython `_speedups` was **built and active** for every stamped run (so these are
fast-path numbers, not pure-Python). Load generator and server ran on the **same
machine** — see the caveat at the end.

## Headline

There is a hard wall between **100 and 250 actively-checking slots.** Below it the
room is healthy; at 250 it falls off a cliff and never recovers. The bottleneck is
**the single asyncio event loop**, not slow Python and not any one feature. Reaching
~1000 slots is an **architectural change (shard the room across loops/processes)**,
not a config tune.

## The sweep

| slots | probe p50 | probe p95 | routing p50 | fanout p99 | items recv | errors | server CPU med/max | RSS |
|------:|----------:|----------:|------------:|-----------:|-----------:|-------:|-------------------:|----:|
| 100   | 1.5 ms    | 60 ms     | 305 ms      | 6.5 ms     | 292        | 0      | 6% / 77%           | 231 MB |
| 250   | 1.7 ms    | 3052 ms   | 8193 ms     | 12385 ms   | 1977       | 0      | 18% / 100%         | 263 MB |
| 500   | 12 ms     | 5000 ms*  | 82970 ms    | dead (n=0) | 781        | 124    | 63% / 100%         | 353 MB |
| 1000  | 120 ms    | 5000 ms*  | 53690 ms    | dead (n=0) | 1008       | 258    | 84% / 100%         | 462 MB |

*5000 ms = the probe's stall ceiling (it gives up and records a 5 s stall).

At 100 everything is flat and clean. At 250 the loop-health probe's median is still
fine (1.7 ms — the loop is healthy *between* events) but its tail explodes to the 5 s
ceiling, item-routing latency jumps to **8 seconds**, datastorage fan-out blows up to
12 s, and by 500+ fan-out stops completing at all and the server starts dropping
connections. Item throughput never keeps up with checks sent past the knee.

## What we ruled in and out

**Cython `_speedups` — already in, not the issue.** The compiled
`_speedups.cp313-win_amd64.pyd` was present and active for all stamped runs, so this
is not a "slow Python location lookups" problem. The wall exists on the fast path.

**Datastorage fan-out (SetNotify) — heavy tax, not the root.** Re-running 250 with
`tracker_fraction 0` (no subscribers) barely moved the stall (probe p95 3052 → 3277 ms,
routing p50 8193 → 6737 ms) — but item delivery jumped ~3.7× (1977 → 7274). So
SetNotify's O(subscribers) broadcast steals a large slice of loop time and its own
latency is catastrophic (12 s, then collapses entirely past 250), but removing it does
not fix the stall.

**The periodic save — real contributor, not the root.** Re-running 250 with
`--disable_save` (no auto-saver thread, no GIL-holding `pickle.dumps`+`zlib`):

| 250 slots | save ON | save OFF |
|---|---:|---:|
| probe p95 | 3052 ms | 2303 ms (−25%) |
| probe p99 / max | 5000 ms | 5000 ms (unchanged) |
| routing p50 | 8193 ms | 6627 ms (−19%) |
| items delivered | 1977 | 7745 (~3.9×) |

Killing the entire save path shaved ~20–25% off the stall and let far more traffic
through — but the loop still stalls past the 5 s ceiling and routing still sits at
**6.6 s**. The save is a slice, not the cause.

**The pattern.** Removing fan-out *or* the save each independently ~4×'d throughput and
shaved ~20% off latency, yet neither fixed the wall. That is the signature of an
**aggregate single-loop ceiling**: each feature is a slice of loop time; remove any one
slice and the loop is still saturated at a few hundred connections.

**Two regimes.** At the 250 knee the server's median CPU is only 12–18% while latencies
are in seconds — it is **not compute-bound** there. That points at serialized I/O /
head-of-line blocking (the loop awaiting hundreds of socket sends in sequence), not raw
computation. Only at 500–1000 does it become genuinely CPU-pegged on one core (median
63 → 84%, single-threaded so 100% = one core maxed).

## Recommendation (ops tune vs rewrite)

The cheap fixes are real but incremental, each worth roughly ~20% / a throughput
multiple, and worth doing regardless:

- **Async / incremental save** — removes the GIL-bound `pickle.dumps`+`zlib` of full
  room state off the hot path; kills the periodic spikes.
- **Relay / shard / debounce the SetNotify path** — the O(subscribers) broadcast is a
  major loop tax and the first thing to die under tracker load.
- **Batch / concurrently dispatch broadcast sends** — the low-CPU/high-latency profile
  at the knee suggests sends are serialized; `asyncio.gather`-ing them could cut
  head-of-line blocking.

But stacking all of these will not reach ~1000 slots. The ceiling is the single event
loop, so the goal requires the **architectural** move: shard one logical room across
multiple loops/processes (e.g., a routing core plus per-shard connection handlers, or a
faster-language reimplementation of the wire/route/notify hot paths with a Python
sidecar for world logic). The harness re-validates any such reimplementation: point it
at the new server, rerun the sweep, compare curves.

## Caveat on the magnitudes

The load generator ran on the **same machine** as the server, and the loop-health probe
lives *inside the generator process*. At the 250 knee the server's CPU is low, so part
of the residual multi-second probe tail could be the co-located generator stalling, not
the server. The **internal deltas** here are trustworthy (save-on vs save-off, fan-out
on vs off — same setup, one variable changed). The **absolute latencies** are not clean
and should not be quoted as the server's true numbers. See `OFFBOX_RUN.md` for the
two-machine run that settles this and produces quotable magnitudes.

---

# Update: the rewrite works (archipela-go vs MultiServer)

The single-event-loop diagnosis above predicted that taking the work off the loop would
remove the wall. It does. `archipela-go` (Go, protocol-compatible skeleton — see
`PROTOCOL_SURFACE.md` / `archipelago-go/`) was scored by the **same harness**.

## Controlled head-to-head @ 250 slots

Everything held constant — same 250 slots, same 25 locations/slot, same harness params,
same box, same `--server-pid` sampling. Both sides drove an **identical 6,225 checks**
(confound removed; this is not "Go did less work"). Python ran with save ON (its real
behavior); Go has no save.

| metric @ 250 (controlled) | Python MultiServer | archipela-go | py/go |
|---|---:|---:|---:|
| probe p95 | 5,000 ms (pegged) | 4.72 ms | ~1,059× |
| probe p99 | 5,000 ms | 22.7 ms | ~220× |
| routing p50 | 15,287 ms | 11.8 ms | ~1,298× |
| routing p95 | 30,147 ms | 29.5 ms | ~1,021× |
| fan-out p99 | 15,547 ms | 17.2 ms | ~902× |
| checks (matched) | 6,225 | 6,225 | — |
| items | 7,553 (15–30 s late) | 6,200 (1:1, on time) | — |
| errors | 0 | 0 | — |
| server CPU med/max | 18% / 101% (one core pegged) | 0% / 37% | — |
| RSS | 271 MB | 114 MB | — |

The rung that pegs Python at 5 seconds is ~5 milliseconds on Go, with the Go server 96%
idle. (Note: this controlled Python run was even worse than the baseline sweep above —
routing p50 15 s vs 8 s — because save-stall timing and the 250-against-1000-room shape
bit harder that run. Python at 250 lands somewhere between "8 s" and "fully pegged"
depending on the day; Go is ~12 ms either way.)

## archipela-go full sweep (synthetic, 50 locs/slot)

| slots | probe p95 | routing p50 | items/checks | fan-out p99 (n) | errors | server CPU med/max |
|------:|----------:|------------:|--------------|-----------------|:------:|-------------------:|
| 100   | 1.4 ms    | 11 ms       | 4900/4950    | 4.7 ms (2.8k)   | 0      | 0% / 37%           |
| 250   | 1.55 ms   | 11 ms       | 12400/12450  | 16.7 ms (28k)   | 0      | 0% / 33%           |
| 500   | 51 ms     | 7.7 ms      | 24900/24950  | 211 ms (125k)   | 0      | 6% / 65%           |
| 1000  | 214 ms    | 103 ms      | 49900/49950  | 492 ms (505k)   | 0      | 18% / 90%          |

archipela-go cleared **1,000 slots** — the population the monolith couldn't survive at
**250** — with zero dropped connections, sub-second routing, fan-out fully alive, and
throughput tracking input 1:1. There is a gentle slope (probe p95 1.4 → 214 ms across the
sweep), but at 1,000 the server's median CPU is only 18% and the probe lives in the
load-generator process — so that top-end creep is most likely the **co-located harness**
saturating one box driving 1,000 sockets, not the server. In other words, we have not yet
found archipela-go's ceiling; the off-box split (`OFFBOX_RUN.md`) is what actually finds
it.

## Verdict

The wall was the GIL-bound single asyncio event loop, confirmed by removing it. The cheap
in-process fixes (async save, notify relay) remain worth ~20% each but were never going to
reach 1,000; the architecture move does. **Federation** (`FEDERATION.md`) ships the
1,000-player experience now on stock servers; **archipela-go** is the path to 1,000 in a
single room, and the same harness scores both — so every step is measured, not asserted.
