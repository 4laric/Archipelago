// session.go — Batch C session/state commands: ConnectUpdate, Sync, StatusUpdate, Say.
//
// Matches MultiServer.py semantics at process_client_cmd (~line 1943–2135):
//   - ConnectUpdate: updates tags mid-session; items_handling is a no-op stub (comment below).
//   - Sync: resends the full received-items list from h.received[slot] at index 0.
//   - StatusUpdate: records client goal/status flag in h.statuses keyed by slot.
//   - Say: broadcasts Chat PrintJSON to all clients; !/ command routing stubbed for Batch E.
//
// PrintJSON is the broadcast workhorse used by all of Batch C (and reused by D/E).
// broadcast() is the lock-snapshot-then-fanout helper shared across batches.
//
// CONCURRENCY RULE (matches existing hub pattern): hold mu only for map ops; snapshot
// under mu, release, then enqueue. Never hold mu across enqueue calls.
package main

import (
	"encoding/json"
	"strings"
)

// ---- PrintJSON & broadcast helpers ----------------------------------------

// printJSON builds a [{"cmd":"PrintJSON","type":typ,"data":parts,...extra}] frame.
//
// typ is the PrintJSON type field (e.g. "Chat", "Join", "TagsChanged", "Goal", …).
// parts is the data array; each element is typically map[string]any{"text":"..."}.
// extra holds any additional top-level fields (e.g. team, slot, tags, message).
// Returns a fully serialised WebSocket frame ready for enqueue.
func (h *Hub) printJSON(typ string, parts []any, extra map[string]any) []byte {
	msg := map[string]any{
		"cmd":  "PrintJSON",
		"type": typ,
		"data": parts,
	}
	for k, v := range extra {
		msg[k] = v
	}
	return frame(msg)
}

// textPart is a convenience constructor for the most common PrintJSON data element.
func textPart(text string) map[string]any {
	return map[string]any{"text": text}
}

// broadcast sends msg to every currently connected client.
// It snapshots h.slotToC under mu, releases the lock, then enqueues to each snapshot —
// exactly the same pattern used for SetReply fan-out in main.go dispatch.
func (h *Hub) broadcast(msg []byte) {
	h.mu.Lock()
	targets := make([]*Client, 0, len(h.slotToC))
	for _, c := range h.slotToC {
		targets = append(targets, c)
	}
	h.mu.Unlock()
	for _, c := range targets {
		c.enqueue(msg)
	}
}

// ---- ConnectUpdate ---------------------------------------------------------

// handleConnectUpdate implements MultiServer.py ConnectUpdate (~line 1943).
//
// Supported fields:
//   - "tags": replaces c.tags; if they changed, broadcasts a TagsChanged PrintJSON
//     (cosmetic, mirrors Python's broadcast_text_all on ConnectUpdate).
//
// "items_handling" NOTE: Python re-evaluates start_inventory + received items and
// re-sends ReceivedItems when items_handling changes. We do not track per-client
// items_handling mode (it is not stored on Client). When present, the field is
// accepted without error (we already apply the broadest handling at Connect time),
// but no re-send is performed. This is conservative: real clients generally send
// ConnectUpdate only to change tags, not items_handling. Implement fully in Batch A
// rework when per-client remote_items / remote_start_inventory tracking is added.
func (h *Hub) handleConnectUpdate(c *Client, cmd map[string]json.RawMessage) {
	// items_handling: no-op stub. Accept the field silently; see comment above.
	// TODO (Batch A): when per-client items_handling is tracked, re-send ReceivedItems here.

	if _, hasTags := cmd["tags"]; !hasTags {
		return
	}

	var newTags []string
	if err := json.Unmarshal(cmd["tags"], &newTags); err != nil || newTags == nil {
		newTags = []string{}
	}

	h.mu.Lock()
	oldTags := c.tags
	changed := !tagSetsEqual(oldTags, newTags)
	if changed {
		c.tags = newTags
	}
	h.mu.Unlock()

	if !changed {
		return
	}

	// Broadcast TagsChanged (cosmetic) — mirrors Python's broadcast_text_all with
	// {"type": "TagsChanged", "team": client.team, "slot": client.slot, "tags": client.tags}.
	msg := h.printJSON("TagsChanged",
		[]any{textPart(slotName(h, c.slot) + " has updated their tags.")},
		map[string]any{
			"team": c.team,
			"slot": c.slot,
			"tags": newTags,
		},
	)
	h.broadcast(msg)
}

// tagSetsEqual reports whether two tag slices contain the same elements (order-insensitive).
// Mirrors Python's `set(old_tags) != set(client.tags)` check.
func tagSetsEqual(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	m := make(map[string]bool, len(a))
	for _, t := range a {
		m[t] = true
	}
	for _, t := range b {
		if !m[t] {
			return false
		}
	}
	return true
}

// slotName returns a human-readable name for a slot: real-mode uses md.SlotInfo[slot].Name,
// synthetic mode falls back to "Player <slot>".
func slotName(h *Hub, slot int) string {
	if h.md != nil {
		if si, ok := h.md.SlotInfo[slot]; ok && si.Name != "" {
			return si.Name
		}
	}
	return "Player"
}

// ---- Sync ------------------------------------------------------------------

// handleSync implements MultiServer.py Sync (~line 1988).
//
// Resends the full accumulated received-items list for c.slot starting at index 0.
// Python: start_inventory + get_received_items → ReceivedItems index:0.
// Here h.received[slot] already contains start inventory (seeded by precollectedItems
// in connectReal) followed by routed items, so we resend the whole slice.
// If the list is empty (slot has received nothing yet), no packet is sent —
// matching Python's `if (start_inventory or items) and not client.no_items`.
func (h *Hub) handleSync(c *Client, _ map[string]json.RawMessage) {
	h.mu.Lock()
	recv := append([]NetworkItem(nil), h.received[c.slot]...)
	h.mu.Unlock()

	if len(recv) == 0 {
		return
	}
	c.enqueue(frame(map[string]any{
		"cmd":   "ReceivedItems",
		"index": 0,
		"items": recv,
	}))
}

// ---- StatusUpdate ----------------------------------------------------------

// handleStatusUpdate implements MultiServer.py StatusUpdate (~line 2121).
//
// Parses the "status" integer and stores it in h.statuses[c.slot].
// Python's update_client_status guards against undoing goal completion
// (current != CLIENT_GOAL check). We mirror that: once a slot reaches status 30
// (CLIENT_GOAL in AP's ClientStatus enum), further StatusUpdate calls that would
// lower it are silently ignored.
//
// NOTE: h.statuses must be declared on Hub and initialised in newHub:
//   statuses map[int]int        // declaration
//   statuses: make(map[int]int) // newHub init
const clientStatusGoal = 30 // AP ClientStatus.CLIENT_GOAL == 30

func (h *Hub) handleStatusUpdate(c *Client, cmd map[string]json.RawMessage) {
	var status int
	if err := json.Unmarshal(cmd["status"], &status); err != nil {
		return
	}

	h.mu.Lock()
	current := h.statuses[c.slot]
	if current != clientStatusGoal {
		h.statuses[c.slot] = status
	}
	h.mu.Unlock()
}

// ---- Say -------------------------------------------------------------------

// handleSay implements MultiServer.py Say (~line 2129) and ClientMessageProcessor.__call__.
//
// Any printable text is broadcast as a Chat PrintJSON to ALL connected clients,
// matching Python's broadcast_text_all with {"type":"Chat","team":...,"slot":...,"message":raw}.
//
// Python's ClientMessageProcessor broadcasts the text first (including for !commands),
// then calls super().__call__ which routes to command handlers. We replicate that order:
// always broadcast the chat, then TODO-stub for !/ commands (Batch E).
//
// Note: Python skips broadcast for "!admin" specifically. We do the same.
func (h *Hub) handleSay(c *Client, cmd map[string]json.RawMessage) {
	text := str(cmd, "text")
	if text == "" {
		return
	}

	// Python: if not raw.startswith("!admin") → broadcast
	if !strings.HasPrefix(text, "!admin") {
		name := slotName(h, c.slot)
		msg := h.printJSON("Chat",
			[]any{textPart(name + ": " + text)},
			map[string]any{
				"team":    c.team,
				"slot":    c.slot,
				"message": text,
			},
		)
		h.broadcast(msg)
	}

	// Route server commands (Batch E): "!cmd" (and "/cmd") → admin handler.
	if strings.HasPrefix(text, "!") || strings.HasPrefix(text, "/") {
		h.handleCommand(c, text)
	}
}
