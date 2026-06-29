# archipela-go — protocol-compatible MultiServer skeleton (Go)

A minimal Archipelago server that speaks the exact wire surface `ap_loadtest.py`
drives, so the **same harness** scores it and you can diff the curves against stock
(Python) MultiServer. This is the `PROTOCOL_SURFACE.md` milestone made runnable.

Like `mock_server.py`, it uses **synthetic** slots/locations (no multidata) on purpose:
it isolates the *server architecture* — Go goroutines, no GIL, non-blocking fan-out —
from generation. Same synthetic load both sides = a clean architecture comparison.

## Build & run

```sh
cd archipelago-go
go mod tidy            # fetches gorilla/websocket
go build -o archipela-go .
./archipela-go --host 0.0.0.0 --port 38281
```

## Score it with the existing harness (no multidata needed)

```sh
# single point
python ap_loadtest.py --host localhost --port 38281 --slots 1000 \
  --check-rate 1 --soak-seconds 180 --tracker-fraction 0.3 --out go_1000.json

# the whole sweep, then compare against the Python baseline from FINDINGS.md
for n in 100 250 500 1000; do
  python ap_loadtest.py --host localhost --port 38281 --slots $n \
    --check-rate 1 --soak-seconds 180 --out go_$n.json
done
python sweep_compare.py go_100.json go_250.json go_500.json go_1000.json
```

The win condition: where stock MultiServer walled at 100–250 slots (probe tail to the
5 s ceiling, routing to tens of seconds, CPU pegging one core), `archipela-go` should
hold flat far past that — and crucially keep climbing because it uses *all* cores.

## The concurrency model (why it should bend the curve)

- **One reader + one writer goroutine per connection**, with a buffered `send` channel
  between them. The hub never blocks on a slow client's socket — it drops a message into
  a channel and moves on; the per-conn writer does the network write in parallel.
- **Fan-out is O(subscribers) channel pushes, not serialized awaits.** On `Set`, the hub
  snapshots subscribers under a short-held lock, *releases the lock*, then enqueues to
  each — so the actual sends happen across many writer goroutines at once. That's exactly
  the head-of-line blocking that serialized at Python's 250-slot knee.
- **Shared room state behind a short-held `sync.Mutex`.** Correct and simple; held only
  for map ops, never across a network write. (The `PROTOCOL_SURFACE.md` single-owning-
  goroutine variant is a drop-in swap if lock contention ever shows up — measure first.)
- **No GIL**, so the `save`/`pickle` GIL-stall that cost ~20% in Python simply doesn't
  exist; a real save runs on its own goroutine without touching the request path.

## Command surface implemented

`RoomInfo` handshake, `Connect`→`Connected`, `LocationChecks`→`ReceivedItems` (cross-slot
routing, source-tagged so the harness pairs send/recv), `LocationScouts`→`LocationInfo`,
`Get`→`Retrieved`, `SetNotify`, `Set`→`SetReply` (concurrent fan-out). That's the full
set `ap_loadtest.py` exercises — probe, routing, throughput, fan-out, reconnect.

## Status & next steps

- **This is a skeleton and is UNBUILT here** (no Go toolchain in the dev sandbox) — build
  it on your machine; `go vet ./...` first. Logic mirrors the validated `mock_server.py`.
- **Synthetic only.** Next real milestone: load an actual multidata — parse the
  `locations` table (`loc -> (item, target_player, flags)`) and `slot_info` from the
  AP multidata so it routes a real room. See `PROTOCOL_SURFACE.md` for the boot artifacts;
  exporting the datapackage/name-groups to JSON at gen time keeps Go free of Python.
- **Datastore semantics** here are replace-only; real AP `Set` has an operation set
  (add, mul, min/max, default, etc.) — straightforward to fill in, none of it is hot-path.
- **Backpressure** currently drops oldest on a full per-client buffer; a production build
  would decide drop-vs-disconnect policy per message type.
