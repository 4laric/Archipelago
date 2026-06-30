# SPEC — Remaining peliarch functions

**Status:** ALL BATCHES DONE — A (loader), B (datastore ops), C (session/PrintJSON), D (GetDataPackage/hints), E (admin), F (save/resume), plus Bounce/DeathLink. Built clean (`go vet`/`build`), 50+ unit tests pass, core commands verified over the wire. Remaining items are refinements only: hint point/cost accounting, multiple clients per slot (trackers), full per-client items_handling re-send.
**Owner:** Alaric
**Source of truth:** `PROTOCOL_SURFACE.md`, `MultiServer.py` @ 0.6.6 (`process_client_cmd` ~line 1843), current `archipelago-go/main.go` (279 lines).
**Goal:** take the synthetic skeleton to a server that routes a **real multidata** with the full command surface, so it can back the Large tier. Ordered by dependency; each item has Go signatures, packet shapes, and acceptance tests.

---

## 0. What exists today (the baseline)

`main.go` implements, against **synthetic** slots/locations:

- `RoomInfo` on connect; `Connect`→`Connected` (auto-assigns an incrementing slot, fabricates `missing_locations`).
- `LocationChecks`→`ReceivedItems` (routes each check to slot `S→(S%n)+1`, source-tagged for the harness).
- `LocationScouts`→`LocationInfo` (echoes the location as its own item).
- `Get`→`Retrieved`, `SetNotify`, `Set`→`SetReply` (replace-only store, concurrent fan-out).

Concurrency model is correct and load-proven: one reader + one writer goroutine per conn, buffered `send` channel, hub state behind a short-held `sync.Mutex` released before fan-out. **Keep this model.** Everything below plugs into it.

**The gap:** no real data. There is no multidata, no datapackage, no auth/version gate, no save/resume, no hints, no admin, no Bounce, no datastore operations beyond replace. The slot/location/item IDs are fabricated, not looked up.

---

## 1. Batch A — the real-multidata loader (unblocks everything real)

This is the gating piece called out in `PROTOCOL_SURFACE.md` and `HOSTING.md §4`. Until it lands, Large tier is "federated under the hood." The runtime needs **no Python** if the static tables are exported once at generation.

### 1.1 Boot artifacts to consume (all static, all exportable)
Per `PROTOCOL_SURFACE.md`:

| Input | Source | Used by |
|---|---|---|
| `locations`: per slot `loc_id → (item_id, target_player, flags)` | multidata | routing core |
| `slot_info` / `player_names` / per-slot game | multidata | identity, Connected, display |
| connect rules: password, min client versions, items_handling | multidata + args | auth gate |
| datapackage: per-game name↔id + checksum | export to JSON once | GetDataPackage, display |
| name groups + hint_blacklist | export to JSON once | hints |
| server options: collect/release/remaining, hint_cost, location_check_points | settings/args | admin + arithmetic |
| initial save (received items, checked, datastore, hints) | `.apsave` / own format | resume |

### 1.2 Go shape
```go
// loaded once at boot, immutable after — no lock needed for reads
type LocationTarget struct {
    Item   int64
    Player int   // target slot
    Flags  int
}
type SlotInfo struct {
    Name string
    Game string
    Type int               // 0=player,1=group,2=item-link
    GroupMembers []int
}
type Multidata struct {
    // locations[slot][locID] -> target
    Locations map[int]map[int64]LocationTarget
    SlotInfo  map[int]SlotInfo
    ConnectNames map[string][2]int   // name -> {team, slot}
    MinVersions  map[int]Version
    Password     string
    Games        map[int]string      // slot -> game
    SlotData     map[int]json.RawMessage
    Options      ServerOptions
}

func LoadMultidata(path string) (*Multidata, error) // parse .archipelago (zip) or a JSON export
```
**Decision:** parse the AP multidata directly in Go, or consume a JSON export produced at generation? `PROTOCOL_SURFACE.md` recommends a one-shot JSON dump of datapackage/name-groups/hint-blacklist so Go never imports `worlds`. Recommended: a `dump_multidata.py` that emits a single Go-friendly JSON bundle (locations table + slot_info + datapackage + name groups + options). Keeps Go free of Python pickle quirks and `restricted_loads`.

**Build:** `dump_multidata.py <room.archipelago> -o room.apgo.json`, then `./peliarch --multidata room.apgo.json`.

### 1.3 Rewrite Connect & LocationChecks against real data
- `Connect`: validate `name ∈ ConnectNames`, password, version ≥ min, items_handling — emit `ConnectionRefused{errors:[...]}` with the exact error strings (`InvalidSlot`, `InvalidPassword`, `IncompatibleVersion`, `InvalidGame`, `InvalidItemsHandling`) per `MultiServer.py:1857`. On success assign the **real** team/slot, send `Connected` with real `missing_locations` (slot's locations minus checked), `checked_locations`, `slot_info`, `slot_data`, `hint_points`.
- `LocationChecks`: dedupe against the slot's checked set; for each *new* loc, look up `Locations[slot][loc] → (item, target, flags)`, append to target's received list, mark checked, emit `ReceivedItems` to the target with the correct running `index`, and `RoomUpdate` with checked_locations. This replaces the synthetic `S→S+1` routing.

### 1.4 Acceptance
- Load a real `.archipelago` (a small 2–4 player ChecksFinder/Clique room from `loadtest_out/`).
- A real AP client (`CommonClient.py`) connects, gets correct `Connected`, checks a location, the **correct** target receives the **correct** item with a monotonic index. Cross-check item/location IDs against the datapackage.
- `LocationScouts` returns the real items at scouted locations (not echoes).

---

## 2. Batch B — datastore operations & subscriptions (correctness on the proven hot path)

The pub/sub plumbing is load-proven; only the **operation set** is stubbed (replace-only). Real AP `Set` applies an operation list. None of it is hot-path-heavy.

### 2.1 Operations to implement
Per `NetUtils`/`MultiServer` datastorage: `replace, default, add, mul, pow, mod, max, min, and, or, xor, left_shift, right_shift, remove, pop, update`. Plus `Set` flags: `want_reply`, and `SetNotify` semantics (subscriber set per key).

```go
func applyOperation(cur json.RawMessage, op string, arg json.RawMessage, dflt json.RawMessage) json.RawMessage
```
- Numeric ops coerce to float64/int as AP does; `default` sets only if absent; `remove`/`pop`/`update` operate on arrays/objects.
- `SetReply` carries `key`, new `value`, **`original_value`** (currently hard-nil — fill it), and echoes `want_reply`/extra keys back. Fan-out to all subscribers + the setter (already concurrent — keep it).
- Special read-only keys AP exposes via Get/SetNotify: `_read_hints_{team}_{slot}`, `_read_item_name_groups_{game}`, `_read_location_name_groups_{game}`, `_read_race_mode`, etc. Implement the `_read_*` namespace as computed reads (back them with hints store + name groups from Batch A/D).

### 2.2 Acceptance
- Unit test each operation against the Python implementation's output for the same input (golden vectors dumped from `MultiServer.py`).
- `original_value` correctly reflects pre-op state. EnergyLink-style `add` accumulation across many setters converges to the same total as Python.

---

## 3. Batch C — session/state commands

| cmd | behavior | notes |
|---|---|---|
| `ConnectUpdate` | change `items_handling` / `tags` mid-session | re-evaluate `no_locations` if tags include HintGame/Tracker/TextOnly (`_non_game_messages`, `MultiServer.py:954`) |
| `Sync` | resend the slot's full received-items list from state | straight read of the received list → `ReceivedItems index:0` |
| `StatusUpdate` | set the client's goal/status flag | store per slot; feeds `/status` admin + goal completion |
| `Say` | chat → `PrintJSON` broadcast | admin `!`/`/` commands branch to Batch E |

`PrintJSON` is the broadcast workhorse — item-send events, chat, hints, countdowns all serialize as `PrintJSON` with a `type` and a `data` parts array. Build one `PrintJSON` emitter with types: `ItemSend`, `Chat`, `Hint`, `Join`, `Part`, `TagsChanged`, `Goal`, `Countdown`, `CommandResult`.

**Acceptance:** real client shows correct join/part/chat; Sync after reconnect restores the full received list; StatusUpdate visible to admin.

---

## 4. Batch D — static-metadata commands

| cmd | needs | behavior |
|---|---|---|
| `GetDataPackage` | exported datapackage + checksums (Batch A) | serve requested games (or all); respond `DataPackage{data:{games:{...}}}` |
| `CreateHints` | name groups + hint_blacklist + hint_cost/points | resolve names→ids (incl. by group), write hints to datastore `_read_hints_{team}_{slot}`, deduct points, `PrintJSON type:Hint` |
| `UpdateHint` | hint status transitions | found/priority/avoid/no-priority transitions in the hints store, with permission checks |

**Acceptance:** client's GetDataPackage checksums match the room's; a hint created by name lands in the correct players' hint lists with correct cost deduction; hint status round-trips.

---

## 5. Batch E — admin / control commands

`/collect`, `/release`, `/remaining`, `/hint`, `/getitem`, `/forfeit`, password-gated ops, countdown. Per `PROTOCOL_SURFACE.md §D` these are **permission checks + the same routing/datastore primitives** already built: `/collect` and `/release` are just `register_location_checks` over all of a slot's locations — pure routing through Batch A. Respect per-room `collect/release/remaining` permission modes from options.

**Acceptance:** `/release` of a finished slot routes all its remaining items to the correct targets; permission modes (`auto`, `enabled`, `disabled`, `goal`) honored.

---

## 6. Batch F — save / resume (durability)

Define peliarch's **own** save format (it need not match `.apsave`; `PROTOCOL_SURFACE.md` says "reimpl defines its own format"). Persist: received-items lists per slot, checked locations, datastore, hints, client statuses. On boot with `--save room.gosave`, restore before accepting connections. A save goroutine snapshots state on a short-held lock and writes off the request path (the no-GIL win — saves never stall routing, unlike Python's pickle/zlib GIL stall).

**Acceptance:** kill -9 mid-room, restart from save, reconnecting clients see identical received lists / checked locations / hints. Save never measurably perturbs routing latency under load (verify in the coverage run).

---

## 7. Cross-cutting

- **Backpressure policy per message type.** Current `enqueue` drops oldest on a full buffer. ReceivedItems/SetReply must **not** be silently dropped (correctness); PrintJSON chat can be. Decide drop-vs-disconnect per type; a slot whose buffer stays full is a dead client → disconnect after a threshold.
- **InvalidPacket** on any malformed/unknown command (currently unknown commands are silently ignored in the `switch`).
- **Single-owning-goroutine variant** (`PROTOCOL_SURFACE.md`) is a drop-in swap if lock contention ever shows in profiling. **Measure first** — the mutex is fine to 4k.

## 8. Ordering & DoD

A (loader) → B (datastore ops) → C (session) → D (metadata) → E (admin) → F (save). Bounce/DeathLink (`SPEC_bounce_deathlink.md`) can land any time after the connection plumbing — it's independent of the loader.

**Done when:** a real multidata room runs end-to-end with real AP clients across all command classes, passes the coverage load run (`SPEC_go_loadtest.md §3.6`), and survives a save/restore. At that point peliarch can back the Large tier for real rooms.
