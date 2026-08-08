# peliarch — protocol-compatible MultiServer skeleton (Go)

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
go build -o peliarch .
./peliarch --host 0.0.0.0 --port 38281
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
5 s ceiling, routing to tens of seconds, CPU pegging one core), `peliarch` should
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

## Real-multidata mode (NEW — Batch A done)

The server now routes an **actual room**, not just synthetic load. Two steps:

```sh
# 1. export a room to a Go-friendly JSON bundle (run from the AP checkout, one dir up)
python archipelago-go/dump_multidata.py loadtest_out/AP_<seed>.zip -o room.apgo.json

# 2. boot the server against it
./peliarch --port 38291 --multidata room.apgo.json
```

In real mode `Connect` does full auth (name→slot lookup, password, min-version, game,
items_handling gates → `ConnectionRefused`), `LocationChecks` does table-lookup cross-slot
routing with per-slot dedupe + a running `ReceivedItems` index + `RoomUpdate`, and
`LocationScouts` returns the real items. Items carry `"class":"NetworkItem"` so real AP
clients (CommonClient) reconstruct them. Without `--multidata` the server stays in the
original **synthetic** mode for architecture load-testing — both modes share the same hub.

Verify end-to-end against a running server:

```sh
python archipelago-go/verify_loader.py --uri ws://127.0.0.1:38291 --bundle room.apgo.json
# drives real clients: handshake, cross-slot routing, scouts, and all auth gates (15 checks)
```

## Command surface implemented

`RoomInfo` handshake, `Connect`→`Connected`, `ConnectUpdate`, `Sync`, `LocationChecks`→
`ReceivedItems` (cross-slot routing, source-tagged so the harness pairs send/recv),
`LocationScouts`→`LocationInfo`, `StatusUpdate`, `Say`→`PrintJSON`, `Bounce`→`Bounced`
(DeathLink/EnergyLink relay), `Get`→`Retrieved`, `SetNotify`, `Set`→`SetReply` with the
**full operation set** (add/mul/pow/mod/max/min/bitwise/remove/pop/update + `original_value`).
That covers everything `ap_loadtest.py` exercises **plus** real-multidata routing, auth
gating, datastore operations, session/chat, and DeathLink.

Now also implemented (batches D–F): `GetDataPackage`→`DataPackage` (served from the
exported datapackage; name groups seeded into `_read_*` keys), `CreateHints`/`UpdateHint`
(hints stored under `_read_hints_{team}_{slot}`, notified to subscribers + PrintJSON),
admin `!release`·`!collect`·`!remaining` (routed via chat, permission-mode aware), and
`--save <path>` durable save/resume with a 30s autosaver running off the request path.

The only remaining gaps are refinements, not core protocol: hint point/cost accounting,
multiple clients per slot (trackers), and full per-client items_handling re-send.

Files: `main.go` (hub + dispatch + routing), `multidata.go` (loader), `datastore.go`
(Set ops), `session.go` (ConnectUpdate/Sync/StatusUpdate/Say + PrintJSON/broadcast),
`bounce.go` (Bounce/DeathLink), `metadata.go` (GetDataPackage + name-group seeding),
`hints.go` (CreateHints/UpdateHint), `admin.go` (release/collect/remaining), `save.go`
(save/resume). Tests: `*_test.go` (`go test ./...`, 50+ cases), plus `run_checks.py`
(end-to-end: routing, Sync, Set ops, DeathLink — 10 checks), `run_checks_tail2.py`
(GetDataPackage/hints/admin), and `verify_loader.py` (loader: 15 checks).

## Status & next steps

- **Builds clean** (`go vet ./...` + `go build`) and passes the 15-check `verify_loader.py`
  acceptance test against the 250-slot ChecksFinder room. Logic mirrors `mock_server.py`
  and `MultiServer.py` @ 0.6.6.
- **Real-multidata loader: DONE** (`dump_multidata.py` + `multidata.go`). Remaining batches
  in `specs/SPEC_remaining_go_functions.md`: datastore operations (B), session cmds (C),
  GetDataPackage/hints (D), admin (E), save/resume (F), plus Bounce/DeathLink.
- **Datastore semantics** here are replace-only; real AP `Set` has an operation set
  (add, mul, min/max, default, etc.) — straightforward to fill in, none of it is hot-path.
- **Backpressure** currently drops oldest on a full per-client buffer; a production build
  would decide drop-vs-disconnect policy per message type. ReceivedItems must not be dropped.
- **Multiple clients per slot** (trackers): one conn per slot today; appending multiple
  endpoints per slot is a refinement noted in the spec.
