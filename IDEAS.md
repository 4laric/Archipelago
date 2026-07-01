# Peliarch — Future Ideas

Parking lot for ideas worth doing later. Not committed work.

---

## Federated DeathLink (DeathLink islands)

**Idea (Alaric, launch day):** Even inside a single 1,000-person mega-room, batch
DeathLink-tagged players into **islands** so a death only links *within* your island,
not across the whole room.

**Why:** Global DeathLink in a mega-room is both chaotic and expensive — one death pings
all 1,000 players (cascade deaths, social noise) and the relay fanout is O(everyone) on
the hot path. Islanding turns it into human-sized death pods (say 4–8 players) while still
hosting everyone in one room.

**How it could work — cheap, builds on what already exists:**
- The `Bounce`→`Bounced` relay already filters by team + games + tags + slots (see
  `bounce.go` / `SPEC_bounce_deathlink.md`). Add an **island** dimension to the DeathLink
  filter:
  - Lightweight: a per-island tag the client carries, e.g. `DeathLink` + `DLIsland:3`, and
    scope the Bounced fanout to same-island DeathLink subscribers.
  - Server-side: an island map (`slot -> island`) and the relay only forwards a DeathLink
    bounce to subscribers in the sender's island.
- **Island assignment:** round-robin/bucket DeathLink players at connect, let players pick,
  or director-assigned — which ties straight into the `FEDERATION.md` director / sub-island
  concept (this is basically that idea applied to the DeathLink relay specifically).
- Still a **pure relay** — the server stays game-agnostic; DeathLink semantics are unchanged
  *within* an island.

**Why it fits Peliarch:** it's a natural **Large-tier** (`peliarch` Go backend) feature —
it makes "1,000 in one room" actually pleasant by giving it sane death/social pods, and it
drops the DeathLink fanout from O(room) to O(island). It's the federation islands concept,
scoped to one relay primitive, with no generation/world changes needed.

**Status:** future idea. Independent of the current Bounce/DeathLink implementation, which
ships global DeathLink today.
