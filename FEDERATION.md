# Federated "islands" model — spec

**Goal:** deliver a ~1000-player shared experience now, on stock servers, by splitting
the population into **islands of size n** (each a self-contained room comfortably under
the scaling wall) linked by a thin **bridge** that provides the global, "we're all here
together" feel. This is the MVP that ships before — and stays compatible with — the Go
rewrite.

Grounding: `FINDINGS.md` puts the healthy zone at **≤100 actively-checking slots**, knee
at 100–250. So an island of **n ≈ 100–150** runs with headroom (plus room for the
bridge's spectator connection and relay traffic). For 1000 players that's **K = 7–10
islands.**

## Topology

```
            ┌─────────── bridge ───────────┐
            │  (spectator client on each)  │
   ┌────────┴───┐   ┌────────────┐   ┌──────┴─────┐
   │ island 1   │   │ island 2   │ … │ island K   │
   │ MultiServer│   │ MultiServer│   │ MultiServer│
   │ ~n slots   │   │ ~n slots   │   │ ~n slots   │
   └────────────┘   └────────────┘   └────────────┘
```

- Each island is an **independent, self-contained multiworld** (its own multidata, its
  own port, its own process). Cross-slot item routing works fully *within* an island —
  it's a real co-op room of n players.
- The **bridge** connects to every island as a **spectator** (`TextOnly`/`Tracker` tag,
  `items_handling=0`) — no playing slot consumed, exactly how community trackers attach.
- Islands are isolated failure domains: one crashing doesn't touch the others. That alone
  is a meaningful ops win over the 1000-slot monolith.

## Two strategies — pick the MVP, keep the door open

### Strategy A — self-contained islands + social bridge  ← **MVP**
Generate each island as its own n-player multiworld, so **items only ever route within an
island.** Cross-island is purely *social*: shared chat, a combined presence/leaderboard,
a global tracker view, optional hint relay. Uses **stock MultiServer unmodified.** Ships
fast.

What you give up: an item found on island A is never destined for a player on island B —
each player's item economy lives inside their island of n. For "1000 people playing
together in a shared, chatty, leaderboarded space," this is usually exactly the product.

### Strategy B — true cross-island item routing
Generate **one** 1000-player multiworld, partition its slots across islands, and have the
bridge route items *between* instances (catch a check on A whose target lives on B, inject
it into B). This preserves full multiworld semantics but requires an **item-injection
hook** stock MultiServer doesn't expose — so it means patching MultiServer or moving
islands onto the Go server (which can offer an `inject` admin command). At that point the
bridge has become the cross-island router — i.e. **Strategy B ≈ the rewrite's router
living in the bridge.** Note: if you've built the Go router you don't need federation for
*scale* anymore; B's niche is only if you want to keep islands as separate failure
domains.

**Recommendation:** ship A. Treat B as the convergence point with `PROTOCOL_SURFACE.md`.

## The bridge (Strategy A) — responsibilities & mechanics

The bridge holds one spectator connection per island and runs these relays. Everything it
needs is already in the wire protocol (no server patch):

| Function | How (per island, via the spectator connection) |
|---|---|
| **Global chat** | read incoming `PrintJSON` chat events; re-`Say` them to the *other* K−1 islands |
| **Presence / leaderboard** | `SetNotify` + `Get` on `_read_client_status_{team}_{slot}` for every slot → aggregate connected/goal-complete into a global board |
| **Global tracker** | `SetNotify` on the read-only/checked-location keys; mirror an aggregate to a global datastore key or a web view |
| **Hint relay (optional)** | watch `_read_hints_{team}_{slot}`; surface cross-island hints as chat |
| **Goal / countdown / events** | rebroadcast milestone `PrintJSON` events (someone finished, "all islands at 80%") |

**Loop prevention (critical):** tag every bridged message with a bridge marker (e.g. a
prefix or a known sender alias) and never re-relay a message that carries it. Otherwise a
chat line ping-pongs across islands forever.

**Scope the relay:** the datastorage notify path is the O(subscribers) cost that collapsed
first under load (`FINDINGS.md`). Keep cross-island mirroring to a small set of curated
keys (status, aggregate progress) — do **not** blanket-mirror every tracker key across
every island.

## Sizing & placement

- `n` = 100 (conservative) to 150 (aggressive). Stay left of the 250 knee with margin for
  the bridge connection + relay broadcasts.
- `K = ceil(total / n)`.
- One box can host several islands (separate processes/ports) until the box's cores fill;
  spread across boxes beyond that. Because islands are independent, this scales
  horizontally with zero shared state on the server side — the bridge is the only thing
  that spans them.
- Put the bridge near (or on) the island boxes; its traffic is chat + a trickle of
  datastorage, not the hot routing path.

## Orchestration (a `federation.py`, mirroring `run_loadtest.py`)

1. **Generate K island multidatas:** `gen_yamls.py` per island (deterministic names, e.g.
   `I{island}P{slot}`), `Generate.py --spoiler 0` each. Independent seeds.
2. **Launch K servers:** one `MultiServer.py <island_k>.zip --port <base+k> --host 0.0.0.0`
   each (reuse the runner's `free_port`, `resolve_server_pid`, `stop_server` helpers).
3. **Launch the bridge:** connect a spectator to each `127.0.0.1:<base+k>`, wire the
   relays above.
4. Players connect to their assigned island's port (a simple director can hand out
   `host:port` round-robin or by cohort).

## Load-testing the federation (reuse the existing harness)

The harness already drives one room. For the federation, point K copies of
`ap_loadtest.py` at the K island ports (n slots each) and run them concurrently — that
reproduces 1000 players across islands and confirms each island stays in its healthy zone
*with the bridge attached* (the bridge adds a spectator + relay load worth measuring).
Watch each island's `sample_server.py` CPU and the per-island probe RTT; the federation
passes if every island looks like the standalone ≤n baseline in `FINDINGS.md`.

## Forward compatibility with the rewrite

The bridge speaks the wire protocol as a client, so an "island" can be a stock
MultiServer today and a Go server (or a shard of one) later with no bridge changes. And
the bridge is the natural home for Strategy B routing once an injection hook exists — so
federation isn't a throwaway MVP, it's the first half of the same architecture.

---

# Extension: constrained placement (friends + access-controlled islands)

Two things real users will ask for the moment this ships: **"put me on an island with
my friend"** and, for streamers, **"make this island subscriber-only."** Both turn island
assignment from round-robin load-balancing into **constrained placement** — keep-together
(affinity) + access policy + capacity — which introduces one new component: the
**director**.

## The director (new component)

A small service that sits in front of the islands and owns *who goes where*. Islands
stay dumb stock MultiServers; all the policy lives here.

Responsibilities:

- **Registration & auth** — identify the player and evaluate access (public, allowlist,
  or a predicate like "is a current Twitch sub" via external OAuth).
- **Party management** — let players form/join a group that must be placed together.
- **Placement** — assign players/parties to islands under affinity + access + capacity.
- **Credential vending** — hand the player back `host:port` + an assigned **slot name** +
  the island's **password**. Unauthorized players never receive credentials.
- **Directory** — keep `player -> (island, slot)` so "follow my friend" and reconnection
  resolve to the right island/slot.
- **Capacity / elasticity** — open another island of a given class when one fills; queue
  or overflow when needed.

Islands need no code changes: stock MultiServer already supports a per-room
**password** (`--password`) and named slots — that's the entire enforcement mechanism the
director leans on. (peliarch can later upgrade the password into a real per-connection
token check; the director's API doesn't change.)

## Slot mechanism (how dynamic join works on baked multidata)

AP slots are fixed in the multidata, so each island is pre-generated with **N generic
slots** (`I{k}P0001..`), exactly like the load-test names. The director assigns an arriving
player to a free generic slot on their target island and tells them which name to connect
as. No per-player regeneration; placement is just bookkeeping over a pool of generic slots.

## Friend grouping (keep-together)

Two models, pick per event:

- **Pre-registration parties** — players form parties during a signup window; the director
  computes the partition *before* generation, then islands are generated sized to the
  result. Deterministic, best for scheduled events.
- **Live follow-a-friend** — islands are pre-provisioned with generic slots; when B joins
  with "follow A," the director looks up A's island and seats B in a free slot there if it
  has room and B passes the island's access policy. Best for open/streamer drop-in.

Rules:

- A party is placed **atomically** into **one** island (reserve `g` slots together).
- `g <= n` must hold. If a party exceeds island size, either cap it, or open an
  oversized island for that party (a deliberate per-island `--island-size` bump).
- **Affinity ⨯ access:** an island admits a party only if **every** member satisfies the
  island's access policy (a mixed sub/non-sub party can't enter a sub-only island; the
  director routes it to the most permissive island that admits all members, or tells the
  party to split).

## Access-controlled islands

- **Policy lives in the director; the mechanism is the island password.** A subscriber-only
  island has a password the director vends *only* to players who pass the predicate.
- **Access classes:** `public` (no gate), `allowlist` (explicit player/token set), and
  `predicate` (live check — subscriber status, role, invite code).
- **Elastic by class:** when a sub-only island fills, open another sub-only island; public
  overflow opens another public island. The director tracks capacity per class.

## Placement algorithm (greedy, feasible-not-optimal)

Same stance as AP's fill (see PROTOCOL_SURFACE / the P-vs-NP discussion): this is a
bin-packing-with-constraints problem, but you only need a **feasible** packing, not an
optimal one, so a greedy pass suffices:

1. Sort parties **largest-first** (first-fit-decreasing).
2. For each party, place it in the first **compatible** island (access admits all members)
   with `>= g` free slots.
3. If none exists, **open a new island of the party's access class** (elastic) and place it
   there; if capacity is hard-capped, queue or reject with a clear reason.
4. Singletons are parties of size 1 — same path, so the algorithm is uniform.

No backtracking needed in the common case; if a pathological set of constraints fails,
the director surfaces *which* constraint (capacity vs access) rather than silently
mis-seating people.

## Bridge & access boundaries (relay scopes)

The bridge gains **relay groups** so chat/presence don't leak across access lines:

- By default, **do not** relay subscriber-island chat into public islands.
- Configurable scopes: subscriber islands can form a private sub-federation; a common
  pattern is **one-way** (sub islands receive the global feed, public islands don't see
  sub-only chat), or fully isolated channels.
- Presence/leaderboard can still aggregate globally (counts) without leaking message
  content — aggregate numbers cross the boundary, raw chat doesn't.

## Interactions & notes

- **Affinity reduces the need for cross-island routing.** Friends who want to interact are
  co-located by placement, so Strategy A (no cross-island item routing) satisfies more of
  the social need than it would under random assignment — a real synergy, not a
  coincidence.
- **Reconnection** is free once the directory exists: a dropped player resolves back to
  their same `(island, slot)`.
- **Forward-compat:** the director is the durable piece. Islands can be stock MultiServer
  today and peliarch shards later; the password mechanism can become a token/auth hook
  in the Go server without touching the director's placement logic.

## Orchestration delta

`federation.py` today generates `K` uniform islands. The constrained model inserts a
**placement step**: (parties + access classes) -> partition -> generate islands sized and
labeled per the partition, with per-island `--password` for gated classes. The director
then runs alongside the bridge, vending credentials against that partition. The load-test
path is unchanged — you still point `K` harnesses at the island ports.
