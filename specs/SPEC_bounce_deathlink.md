# SPEC — Bounce relay & DeathLink in peliarch

**Status:** implementation-ready
**Owner:** Alaric
**Source of truth:** `MultiServer.py` @ 0.6.6, `Bounce` handler (~line 2137); tags model (`_non_game_messages`, line 954); `process_client_cmd` Connect/ConnectUpdate tag handling.
**Goal:** implement the `Bounce`/`Bounced` relay so DeathLink, EnergyLink, and other tag/game/slot-scoped client-to-client messages work. DeathLink is the headline use; the server itself stays game-agnostic — it's a pure relay.

---

## 1. The key insight: the server does not know what DeathLink is

DeathLink is **not** a server feature. It is a convention layered on top of the generic `Bounce` relay: clients tagged `"DeathLink"` send a `Bounce` with `tags:["DeathLink"]` and a `data` payload (`{time, cause, source}`); the server relays it to **other** clients that also carry the `DeathLink` tag on the same team. The server never inspects or validates the payload — it routes the envelope and forwards the blob verbatim.

This means **one correct `Bounce` implementation gives DeathLink, EnergyLink, and any future tag-based link for free.** The only DeathLink-specific work is making sure tags round-trip correctly through Connect/ConnectUpdate so the filter matches.

---

## 2. Exact relay semantics (from MultiServer.py:2137)

```python
elif cmd == "Bounce":
    games = set(args.get("games", []))
    tags  = set(args.get("tags", []))
    slots = set(args.get("slots", []))
    args["cmd"] = "Bounced"
    msg = ctx.dumper([args])
    for bounceclient in ctx.endpoints:
        if client.team == bounceclient.team and (
            ctx.games[bounceclient.slot] in games or
            set(bounceclient.tags) & tags or
            bounceclient.slot in slots):
            await ctx.send_encoded_msgs(bounceclient, msg)
```

Rules to preserve exactly:

1. **Team scope is mandatory.** Only endpoints on the **same team** as the sender are eligible. (Single-team rooms = everyone, but the check must exist for multi-team.)
2. **Match is the union of three filters** — an endpoint receives if **any** holds: its game ∈ `games`, OR its tags ∩ `tags` ≠ ∅, OR its slot ∈ `slots`.
3. **The sender is NOT excluded.** Python iterates all endpoints including the originator. If a DeathLink sender also carries the `DeathLink` tag, it receives its own bounce back. Match Python — clients dedupe by `source`. (Do **not** add a "skip self" the Python server doesn't have.)
4. **Payload is forwarded verbatim.** The whole `args` object is re-emitted with `cmd` flipped to `"Bounced"`; every other key (`data`, `tags`, `games`, `slots`) passes through untouched. Do not parse `data`.
5. **One endpoint, possibly many slots.** AP keys the filter on per-connection `slot`/`tags`/`game`. peliarch is one-slot-per-connection today, so endpoint == slot; keep that mapping.

---

## 3. Go implementation

### 3.1 Client needs team, tags, game
Extend `Client` (today it only has `slot`):
```go
type Client struct {
    conn *websocket.Conn
    slot int
    team int
    tags []string          // from Connect/ConnectUpdate args["tags"]
    game string            // from multidata slot_info (Batch A) or "" synthetic
    send chan []byte
    done chan struct{}
}
```
Tags are set on `Connect` (`client.tags = args['tags']`, `MultiServer.py:1904`) and mutated on `ConnectUpdate` (line 1977). Until the real-multidata loader lands, `game` can be the synthetic room game; `team` defaults to 0. **Bounce does not depend on Batch A** — it works in the synthetic server with tags alone.

### 3.2 Dispatch case
```go
case "Bounce":
    var games, tags []string
    var slots []int
    json.Unmarshal(cmd["games"], &games)
    json.Unmarshal(cmd["tags"], &tags)
    json.Unmarshal(cmd["slots"], &slots)
    gameSet := toSet(games); tagSet := toSet(tags); slotSet := toIntSet(slots)

    // rebuild the envelope as Bounced, preserving all original keys
    out := map[string]json.RawMessage{}
    for k, v := range cmd { out[k] = v }
    out["cmd"] = json.RawMessage(`"Bounced"`)
    msg := frame(rawObj(out))   // single Bounced command in a frame

    h.mu.Lock()
    var targets []*Client
    for _, other := range h.allClients() {        // snapshot under lock
        if other.team != c.team { continue }
        if gameSet[other.game] || intersects(other.tags, tagSet) || slotSet[other.slot] {
            targets = append(targets, other)
        }
    }
    h.mu.Unlock()                                  // release BEFORE fan-out

    for _, t := range targets { t.enqueue(msg) }   // concurrent, like Set
```

Notes:
- **Snapshot the matching set under the short-held lock, release, then `enqueue`** — identical discipline to the proven `Set` fan-out. Bounce is O(matching endpoints) channel pushes; writes run in parallel.
- The hub needs an endpoint registry. Today `slotToC map[int]*Client` only holds connected slots; that's fine as the iteration source (`allClients()` = values of `slotToC`). Endpoints that connected but pre-date slot assignment (RoomInfo-only) can't match game/slot anyway and rarely carry tags — iterating assigned slots matches Python's `ctx.endpoints` closely enough; revisit if a tag-only pre-Connect client ever needs bounces.
- **Backpressure:** DeathLink is correctness-sensitive within a session but is itself transient (a death you missed is gone). Treat Bounced like ReceivedItems for buffer policy — prefer disconnecting a persistently-full client over silently dropping, but a single drop under overload is acceptable (matches a missed death IRL). Decide in the cross-cutting backpressure work (`SPEC_remaining_go_functions.md §7`).

### 3.3 Tag round-trip (the only DeathLink-specific glue)
- `Connect`: parse and store `args["tags"]` on the client. The current skeleton ignores tags entirely — add it.
- `ConnectUpdate`: implement (currently absent) so a client can add/remove `DeathLink`/`EnergyLink` mid-session; mutate `client.tags` under the lock.
- Optional: broadcast `PrintJSON`/`TagsChanged` on tag change (cosmetic; not required for DeathLink to function).

---

## 4. Acceptance tests

1. **Two-client DeathLink loop (synthetic server, no multidata needed):**
   - Client A and B both Connect with `tags:["DeathLink"]`.
   - A sends `{"cmd":"Bounce","tags":["DeathLink"],"data":{"time":...,"cause":"A died","source":"A"}}`.
   - **B receives a `Bounced` with identical `data`.** A also receives it back (Python parity) and ignores it by `source`.
2. **Tag filter excludes non-DeathLink:** a third client C with `tags:[]` receives nothing.
3. **Game filter:** Bounce with `games:["Clique"]` reaches all Clique slots regardless of tags.
4. **Slot filter:** Bounce with `slots:[3]` reaches only slot 3.
5. **Union semantics:** Bounce with both `tags:["DeathLink"]` and `slots:[5]` reaches DeathLink-tagged clients *and* slot 5 (even if slot 5 is untagged), with no duplicate delivery to a client matching two filters (each endpoint enqueued once — dedupe the target list).
6. **Team isolation:** in a 2-team room, a Bounce from team 0 never reaches team 1.
7. **Verbatim payload:** arbitrary nested `data` survives the round-trip byte-for-byte.
8. **Load:** add a DeathLink broadcast profile to the coverage run (`SPEC_go_loadtest.md §3.6`) — a fraction of slots emit a DeathLink Bounce each soak second; Bounced tail stays bounded and doesn't starve routing.

Verify #1–#7 against a real AP client (or `CommonClient.py` with a DeathLink-tagged connection) and, where possible, diff behavior against stock `MultiServer.py` for the same inputs.

## 5. DoD
`Bounce`/`Bounced` implemented with team+games+tags+slots union filter, sender-inclusive, verbatim payload, concurrent fan-out; tags round-trip through Connect/ConnectUpdate; the two-client DeathLink loop passes against a real client; the load profile is added. DeathLink, EnergyLink, and future tag-links all work with no further server code.
