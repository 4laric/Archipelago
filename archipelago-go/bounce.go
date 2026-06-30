// bounce.go — Bounce/Bounced relay (DeathLink, EnergyLink, tag/game/slot fanout).
//
// Matches MultiServer.py:2137 exactly:
//   - Team-scoped: only clients on the same team as the sender are eligible.
//   - Union filter: a client matches if any of (game ∈ games, tags ∩ tags ≠ ∅, slot ∈ slots) holds.
//   - Sender is NOT excluded (Python iterates all endpoints including the originator).
//   - Payload forwarded verbatim: all original keys preserved, only "cmd" flipped to "Bounced".
//   - Targets snapshotted under mu, mu released, then enqueue called outside the lock.
package main

import "encoding/json"

// handleBounce implements the Bounce command from MultiServer.py:2137.
// It relays the Bounced envelope to every connected client on the sender's team
// whose game, tags, or slot matches the Bounce filter (union semantics).
func (h *Hub) handleBounce(c *Client, cmd map[string]json.RawMessage) {
	// Parse filter sets from the incoming command (any may be absent/null).
	var gameList []string
	var tagList []string
	var slotList []int
	json.Unmarshal(cmd["games"], &gameList)
	json.Unmarshal(cmd["tags"], &tagList)
	json.Unmarshal(cmd["slots"], &slotList)

	games := toStringSet(gameList)
	tags := toStringSet(tagList)
	slots := toIntSet(slotList)

	// Rebuild envelope: copy all keys verbatim, flip "cmd" to "Bounced".
	out := make(map[string]json.RawMessage, len(cmd)+1)
	for k, v := range cmd {
		out[k] = v
	}
	out["cmd"] = json.RawMessage(`"Bounced"`)
	msg := frame(rawMap(out))

	// Snapshot matching clients under the lock, then release before enqueuing.
	h.mu.Lock()
	var targets []*Client
	for _, other := range h.slotToC {
		if bounceMatches(
			h.gameFor(other.slot),
			other.tags,
			other.slot,
			other.team,
			c.team,
			games, tags, slots,
		) {
			targets = append(targets, other)
		}
	}
	h.mu.Unlock()

	// Fan-out: each target gets its own enqueue (non-blocking channel push).
	for _, t := range targets {
		t.enqueue(msg)
	}
}

// gameFor returns the game name for a slot. In real mode it consults md.SlotInfo;
// in synthetic mode it returns "Clique" (the synthetic game), matching connectSynthetic.
// Called under h.mu (read-only access to immutable md is safe without a separate lock).
func (h *Hub) gameFor(slot int) string {
	if h.md != nil {
		if si, ok := h.md.SlotInfo[slot]; ok {
			return si.Game
		}
		return ""
	}
	return "Clique"
}

// bounceMatches is a pure, unit-testable predicate that mirrors the Python filter:
//
//	ctx.games[bounceclient.slot] in games or
//	set(bounceclient.tags) & tags or
//	bounceclient.slot in slots
//
// and the outer team guard:
//
//	client.team == bounceclient.team
func bounceMatches(
	game string,
	clientTags []string,
	clientSlot int,
	clientTeam int,
	senderTeam int,
	games map[string]bool,
	tags map[string]bool,
	slots map[int]bool,
) bool {
	if clientTeam != senderTeam {
		return false
	}
	// All three filter sets empty => no match (Python: empty set membership / intersection are falsy).
	if len(games) == 0 && len(tags) == 0 && len(slots) == 0 {
		return false
	}
	if games[game] {
		return true
	}
	for _, t := range clientTags {
		if tags[t] {
			return true
		}
	}
	if slots[clientSlot] {
		return true
	}
	return false
}

// toStringSet converts a string slice to a presence map.
func toStringSet(ss []string) map[string]bool {
	if len(ss) == 0 {
		return map[string]bool{}
	}
	m := make(map[string]bool, len(ss))
	for _, s := range ss {
		m[s] = true
	}
	return m
}

// toIntSet converts an int slice to a presence map.
func toIntSet(is []int) map[int]bool {
	if len(is) == 0 {
		return map[int]bool{}
	}
	m := make(map[int]bool, len(is))
	for _, i := range is {
		m[i] = true
	}
	return m
}

// rawMap wraps a map[string]json.RawMessage so it marshals back to a JSON object.
// We need this because frame(cmds ...any) calls json.Marshal on its arguments, and
// map[string]json.RawMessage already implements json.Marshaler correctly.
func rawMap(m map[string]json.RawMessage) map[string]json.RawMessage { return m }
