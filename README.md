s# AP single-room scaling harness

Find the breakpoint for **one large Archipelago room** (toward ~1000 slots) — the
streamer community-sync case. This drives a real, unmodified `MultiServer.py` over
the wire and measures the paths most likely to fail at scale. It does **not** test
generation (separate fill/logic problem).

## Files
- `ap_loadtest.py` — the harness (client swarm + measurement).
- `gen_yamls.py` — emits N Clique YAMLs with deterministic slot names.
- `mock_server.py` — fake server for self-testing the harness only (ignore for real runs).

## What it measures, and why
| Metric | Path under test | The wall it reveals |
|---|---|---|
| `loop_health_probe_rtt_ms` | dedicated reserved slot pinging `Get`→`Retrieved` | single event-loop stalls, esp. periodic `save()` stop-the-world |
| `routing_latency_ms` | check a location → `ReceivedItems` on the recipient (self or cross-slot) | item-routing latency under load, including cross-connection push |
| `throughput` (checks vs items) + timeseries | sustained `LocationChecks` | server falling behind (lag grows = breakpoint) |
| `fanout_latency_ms` | many `SetNotify` subscribers, few `Set` | datastorage `SetNotify` O(subscribers) fan-out |
| `reconnect` time + `backlog_items` | drop a fraction, rejoin at once | reconnect-storm cost + full `ReceivedItems` backlog resend |
| server CPU%/RSS (with `--server-pid`) | psutil sampling | CPU saturation, memory creep |

The harness owns every slot, so it correlates routing latency by `(source_slot,
source_location)` — when slot A checks a location whose item routes to slot B, it
pairs A's send-time with B's receive-time across the two connections. Works for self-
and cross-slot routing on any multiworld. It reserves the **last** slot for the
loop-health probe.

## Run it for real
1. Generate slots and multidata (in your Archipelago checkout):
   ```
   python gen_yamls.py --count 1000 --out ./loadtest_players
   python Generate.py --player_files_path ./loadtest_players --outputpath ./loadtest_out
   ```
2. Host the resulting output with stock MultiServer, note the port (default 38281).
   Grab its PID for resource sampling: `pgrep -f MultiServer`.
3. Drive it:
   ```
   python ap_loadtest.py --host localhost --port 38281 \
     --slots 1000 --check-rate 1 --soak-seconds 180 \
     --tracker-fraction 0.3 --reconnect-fraction 0.25 \
     --server-pid <PID> --out results.json
   ```
   `--version` must be protocol-compatible with your server (`major,minor,build`);
   bump it if you get `ConnectionRefused: InvalidVersion`. For `archipelago.gg`-style
   TLS hosting you'd need `wss://`; local self-hosted is plain `ws://` (what this uses).

## Reading results — the breakpoint signals
- **Probe RTT p95/p99 climbing with slot count** = the loop is saturating. A periodic
  spike to ~save-duration = the synchronous save is your stall; everything else flat
  but periodic blips means save cadence is the first thing to fix (async/incremental save).
- **`items_recv` falling behind `checks_sent`** in the timeseries (growing gap) = the
  server can't keep up with routing. That gap-vs-slots curve is your scaling ceiling.
- **`fanout_latency` p99 exploding while soak metrics stay flat** = datastorage
  `SetNotify` is the bottleneck, not item routing. Fix = relay/shard the notify path,
  or debounce tracker writes. This is often the real killer with many trackers.
- **`reconnect.time_to_connected` p95 and `backlog_items` large** = reconnect storms
  are expensive; the full backlog resend is the cost. Fix = incremental resync.
- **RSS climbing monotonically** over a long soak = memory creep worth chasing.

Sweep `--slots` (e.g. 100 / 250 / 500 / 1000) holding `--check-rate` fixed and plot
each metric vs slot count. The knee in those curves is the number you take into the
"ops-tuning vs. rewrite" decision.

## Tuning the load shape
- `--check-rate` — checks per client per second (aggregate = slots × rate).
- `--tracker-fraction` / `--set-rate` / `--fanout-seconds` — datastorage stress.
- `--reconnect-fraction` — size of the simulated reconnect storm.

## Choosing the game (and why not 1000× Hollow Knight)
- **Don't** lead with a heavy game like Hollow Knight at 1000 slots. That's the
  *generation* bottleneck reintroduced — likely 30+ min and an OOM risk before you
  get a multidata — and it couples "heavy game" with "many slots," so a bad number
  won't tell you which caused it.
- **For the sweep**, use a light game that still shuffles items cross-slot so the
  routing-latency metric is real: `ChecksFinder` (no ROM, modest locations, genuine
  item economy) is a good default. The harness measures cross-slot routing on any
  multiworld, so you don't need a heavy game to exercise the push path.
- **Optional realism anchor**: if you want true per-slot memory + heavy-routing cost,
  generate ONE multidata with a heavy game at a *small* slot count (100–200), measure,
  and combine with the connection-scaling curve from the light-game sweep. You almost
  never need 1000× a heavy game.

## Caveats
- Clique self-routes its one item, so it won't exercise the cross-connection push
  path even though the harness can measure it. Use a light game that routes items
  across slots (e.g. ChecksFinder) if you want routing-latency numbers; see the
  game-choice section above.
- 1000 sockets from one box is fine (IO-bound), but if the harness host CPU-saturates
  at very high check rates, split clients across machines so you measure the *server*,
  not the load generator.
- This measures stock AP. The same harness validates any protocol-compatible
  reimplementation later — point it at the new server, rerun the sweep, compare curves.
