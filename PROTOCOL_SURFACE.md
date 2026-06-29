# Runtime protocol surface — what a fast MultiServer must implement

Source of truth: `MultiServer.py` @ 0.6.6, `process_client_cmd` dispatch (line ~1843)
and `register_location_checks` (line ~1132).

## Mental model

At runtime the server is **not a logic engine — it's a router over a frozen dataset
plus a key/value pub-sub plus a chat/relay bus.** Everything game-specific is either
baked into the multidata at generation time or is static metadata loaded once at boot.
The routing core is literally a table lookup:

```
slot_locations[location] -> (item_id, target_player, flags)   # from the multidata
-> NetworkItem -> send to target_player's connection
```

`ctx.locations` is a `LocationStore` built directly from `decoded_obj["locations"]`.
There is **no call into world code on any client command.** That's the whole reason a
non-Python reimplementation is tractable: the hard, Python-only part (fill/logic) ran at
generation and is already frozen.

## What the server needs at boot (all static, all exportable)

A reimplemented server consumes these as data artifacts — no `import worlds` required if
they're exported once:

| Input | Source today | Notes |
|---|---|---|
| `locations`: per slot, `loc_id -> (item_id, target_player, flags)` | multidata | the routing table; this *is* the room |
| `slot_info` / `player_names` / per-slot game | multidata | identity + display |
| connect rules: password, min client versions, items_handling | multidata + args | auth/gate |
| **datapackage**: per game, name↔id maps + checksum | `worlds` pkg | display + `GetDataPackage`; export to JSON once |
| item/location **name groups** + **hint_blacklist** | `worlds` pkg | only for hint-by-group + display |
| server options: collect/release/remaining modes, `hint_cost`, `location_check_points` | settings/args | config + arithmetic |
| initial **save**: received-items lists, checked locations, datastore, hints | `.apsave` | resume state; reimpl defines its own format |

The only things that currently come from importing `worlds` are the **datapackage, name
groups, and hint blacklist** — all static lookup tables. Export them to JSON at
generation (or via a one-shot dump) and the runtime server never touches Python worlds.

## Command surface, sorted by what it actually needs

**A — pure routing / connection (no world code, no metadata):**

| cmd | does |
|---|---|
| `Connect` | auth, version gate, assign slot, send `Connected` + initial `ReceivedItems` |
| `ConnectUpdate` | change items_handling / tags |
| `Sync` | resend the slot's full received-items list (from save) |
| `LocationChecks` | the hot path: dedupe new locations, table-lookup → route items, mark checked, `RoomUpdate` |
| `LocationScouts` | table-lookup items at locations → `LocationInfo` |
| `StatusUpdate` | set client goal/status flag |
| `Bounce` | relay a blob to clients matching games/slots/tags |
| `Say` | chat → `PrintJSON` broadcast (admin `!`/`/` commands handled separately) |

**B — datastorage (key/value + O(subscribers) pub/sub):**

| cmd | does |
|---|---|
| `Get` | read keys → `Retrieved` |
| `Set` | apply operations to a key, `SetReply` to setter + **all subscribers** |
| `SetNotify` | subscribe a connection to keys |

This is the path that died first under load. In the rewrite it's a concurrent pub/sub —
the single biggest win versus Python's serialized awaits.

**C — needs static metadata only (no live logic):**

| cmd | needs |
|---|---|
| `GetDataPackage` | serve the exported datapackage (+ checksums) |
| `CreateHints` | name groups + hint_blacklist + `hint_cost`/points → writes hints to datastore |
| `UpdateHint` | hint status transitions in datastore |

**D — admin / control (`/collect`, `/release`, `/remaining`, `/hint`, …):**
permission checks + the same routing/datastore primitives as above. `/collect` and
`/release` are just `register_location_checks` over all of a slot's locations — still
pure routing.

## Server → client packets to emit

`RoomInfo` (pre-Connect handshake), `Connected`, `ConnectionRefused`, `ReceivedItems`,
`LocationInfo`, `RoomUpdate`, `PrintJSON` (chat, item-send events, hints, countdowns),
`DataPackage`, `Bounced`, `SetReply`, `Retrieved`, `InvalidPacket`.

## The sidecar verdict

With the datapackage/name-groups/hint-blacklist exported to data, **the runtime fast
path needs no Python at all.** Nothing in `process_client_cmd` calls world code. So the
"Python sidecar" is *optional* — reserve it only for future world-specific runtime hooks
(none exist in core today) or to reuse Python for the admin/management console. The fast
server can run standalone against the multidata + a metadata export.

## Concurrency model (the point of the exercise)

Run socket I/O fully parallel (one goroutine/task per connection) and keep room
mutations correct by funneling them through a **single owning goroutine via a channel**
(or a sharded lock keyed by team). That preserves the "one consistent room" semantics
cross-slot routing depends on, while letting hundreds/thousands of sockets read and write
in parallel — exactly what the GIL denied the Python loop. Broadcasts (`ReceivedItems`,
`SetReply`, `PrintJSON`) fan out concurrently instead of `await`-ing each socket in turn,
which is the head-of-line blocking we measured at the 250 knee.

## Validation hook

The load harness speaks this wire protocol, not Python internals. First milestone that
unlocks measurement: a server that completes `RoomInfo → Connect → Connected` and answers
`Get`. At that point `ap_loadtest.py --host <newserver>` runs the loop-health probe; add
`LocationChecks` + `ReceivedItems` and the full sweep runs. Compare curves against the
stock-MultiServer baseline in `FINDINGS.md` — the rewrite has to bend those curves to
earn its keep.
