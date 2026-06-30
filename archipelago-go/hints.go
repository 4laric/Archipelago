// hints.go — CreateHints and UpdateHint commands for peliarch.
//
// Implements Batch D (hints subset) of specs/SPEC_remaining_go_functions.md.
//
// Hint model mirrors NetUtils.Hint (a NamedTuple):
//
//	Hint(receiving_player, finding_player, location, item, found, entrance, item_flags, status)
//
// When stored in h.store or broadcast via PrintJSON the struct is serialised with
// "class":"Hint" so AP clients reconstruct it as the expected namedtuple-as-dict.
//
// Per-slot hint lists live in h.store under the key "_read_hints_{team}_{slot}".
// This server is single-team (team 0 for all real-mode connections; synthetic clients
// are also team 0).
//
// Concurrency model (unchanged from the rest of the package):
//
//	hold h.mu only for map reads/writes; snapshot subscribers under mu, release,
//	then enqueue. Never hold mu across enqueue calls.
package main

import (
	"encoding/json"
	"fmt"
)

// ---- HintStatus constants (NetUtils.HintStatus IntEnum) ---------------------

const (
	hintStatusUnspecified = 0  // HINT_UNSPECIFIED
	hintStatusNoPriority  = 10 // HINT_NO_PRIORITY
	hintStatusAvoid       = 20 // HINT_AVOID
	hintStatusPriority    = 30 // HINT_PRIORITY
	hintStatusFound       = 40 // HINT_FOUND
)

// hintStatusValid reports whether status is a recognised HintStatus value.
func hintStatusValid(status int) bool {
	switch status {
	case hintStatusUnspecified, hintStatusNoPriority, hintStatusAvoid, hintStatusPriority, hintStatusFound:
		return true
	}
	return false
}

// ---- Hint struct ------------------------------------------------------------

// Hint mirrors NetUtils.Hint (NamedTuple). Field order matches the Python definition:
//
//	Hint(receiving_player, finding_player, location, item, found, entrance, item_flags, status)
//
// MarshalJSON injects "class":"Hint" so real AP clients reconstruct the namedtuple.
type Hint struct {
	ReceivingPlayer int    `json:"receiving_player"`
	FindingPlayer   int    `json:"finding_player"`
	Location        int64  `json:"location"`
	Item            int64  `json:"item"`
	Found           bool   `json:"found"`
	Entrance        string `json:"entrance"`
	ItemFlags       int    `json:"item_flags"`
	Status          int    `json:"status"`
}

func (hint Hint) MarshalJSON() ([]byte, error) {
	// Use an alias to avoid infinite recursion, then inject the class field.
	type alias Hint
	type withClass struct {
		Class string `json:"class"`
		alias
	}
	return json.Marshal(withClass{Class: "Hint", alias: alias(hint)})
}

// hintEqual reports whether two Hints are the same hint identity (matching
// Python's __hash__: receiving_player, finding_player, location, item, entrance).
// Status and found are mutable metadata, not identity fields.
func hintEqual(a, b Hint) bool {
	return a.ReceivingPlayer == b.ReceivingPlayer &&
		a.FindingPlayer == b.FindingPlayer &&
		a.Location == b.Location &&
		a.Item == b.Item &&
		a.Entrance == b.Entrance
}

// ---- Per-slot hint list store helpers --------------------------------------

// hintKey returns the datastore key for a slot's hint list.
func hintKey(team, slot int) string {
	return fmt.Sprintf("_read_hints_%d_%d", team, slot)
}

// hintLoadList reads the hint list for (team, slot) from h.store.
// Caller must hold h.mu OR call only on its own goroutine with a snapshot key.
// Returns nil if the key is absent.
func hintLoadList(raw json.RawMessage) []Hint {
	if isNull(raw) {
		return nil
	}
	// The list is stored as an array of Hint-shaped objects (with "class":"Hint").
	// We decode into a helper struct that tolerates the extra "class" field.
	var items []struct {
		ReceivingPlayer int    `json:"receiving_player"`
		FindingPlayer   int    `json:"finding_player"`
		Location        int64  `json:"location"`
		Item            int64  `json:"item"`
		Found           bool   `json:"found"`
		Entrance        string `json:"entrance"`
		ItemFlags       int    `json:"item_flags"`
		Status          int    `json:"status"`
	}
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil
	}
	out := make([]Hint, 0, len(items))
	for _, it := range items {
		out = append(out, Hint{
			ReceivingPlayer: it.ReceivingPlayer,
			FindingPlayer:   it.FindingPlayer,
			Location:        it.Location,
			Item:            it.Item,
			Found:           it.Found,
			Entrance:        it.Entrance,
			ItemFlags:       it.ItemFlags,
			Status:          it.Status,
		})
	}
	return out
}

// hintMarshalList serialises a []Hint into a json.RawMessage ready for h.store.
func hintMarshalList(hints []Hint) json.RawMessage {
	b, _ := json.Marshal(hints)
	return b
}

// hintAddUnique appends hint to list only if an identical hint (by identity
// fields) is not already present. Returns the (possibly unchanged) list and
// whether a new hint was added.
func hintAddUnique(list []Hint, hint Hint) ([]Hint, bool) {
	for _, existing := range list {
		if hintEqual(existing, hint) {
			return list, false
		}
	}
	return append(list, hint), true
}

// ---- handleCreateHints -----------------------------------------------------

// handleCreateHints implements the AP "CreateHints" command (MultiServer.py ~line 2025).
//
// Parses "locations" ([]int64 in the requesting slot's location space), looks up
// each location in md.Locations, builds Hint structs, de-duplicates against the
// existing per-slot lists, persists to h.store, and fans out SetReply + PrintJSON
// notifications.
//
// "player" field (off-world hinting) and cost deduction are not yet implemented;
// for now only locations == c.slot paths are accepted (the common case for
// tracker / client auto-hinting).
//
// TODO: hint points/cost — deduct md.Options.HintCost per new hint from a
// "hint_points_{team}_{slot}" store key once the points ledger is wired up.
func (h *Hub) handleCreateHints(c *Client, cmd map[string]json.RawMessage) {
	if h.md == nil {
		// Synthetic mode has no routing table; hints are not meaningful.
		return
	}

	locs := ints64(cmd, "locations")
	if len(locs) == 0 {
		c.enqueue(frame(map[string]any{
			"cmd": "InvalidPacket", "type": "arguments",
			"text": "CreateHints: No locations specified.", "original_cmd": cmd,
		}))
		return
	}

	// TODO: "player" field — support off-world hinting (location_player != c.slot).
	// For now we always use c.slot as the finding player, matching the common path.
	findingPlayer := c.slot
	team := c.team

	table := h.md.Locations[findingPlayer]

	// Build the new hints from the routing table.
	var newHints []Hint
	for _, loc := range locs {
		tgt, known := table[loc]
		if !known {
			continue // location not in this slot's table — skip silently
		}

		h.mu.Lock()
		found := h.checked[findingPlayer][loc]
		h.mu.Unlock()

		status := hintStatusUnspecified
		if found {
			status = hintStatusFound
		}

		newHints = append(newHints, Hint{
			ReceivingPlayer: tgt.Player,
			FindingPlayer:   findingPlayer,
			Location:        loc,
			Item:            tgt.Item,
			Found:           found,
			Entrance:        "",
			ItemFlags:       tgt.Flags,
			Status:          status,
		})
	}

	if len(newHints) == 0 {
		return
	}

	// Determine which slots' hint lists are affected (finder + each unique receiver).
	// Then, under ONE lock acquisition, load all affected lists, add unique hints,
	// write back, and snapshot subscriber sets.
	type slotUpdate struct {
		key  string
		list []Hint
	}

	// Collect the set of slots that need updating.
	affectedSlots := make(map[int]struct{})
	affectedSlots[findingPlayer] = struct{}{}
	for _, hint := range newHints {
		affectedSlots[hint.ReceivingPlayer] = struct{}{}
	}

	// Under the lock: load lists, merge, write back, snapshot subs.
	h.mu.Lock()
	updates := make(map[int]slotUpdate, len(affectedSlots))
	for slot := range affectedSlots {
		key := hintKey(team, slot)
		existing := hintLoadList(h.store[key])
		for _, hint := range newHints {
			// Add this hint to any slot that has a stake in it
			// (finder gets all hints; receiver gets hints where it is the receiver).
			if slot == findingPlayer || slot == hint.ReceivingPlayer {
				existing, _ = hintAddUnique(existing, hint)
			}
		}
		updates[slot] = slotUpdate{key: key, list: existing}
	}
	// Write updated lists back to store.
	for _, upd := range updates {
		h.store[upd.key] = hintMarshalList(upd.list)
	}
	// Snapshot subscriber sets for each affected key.
	type notifyEntry struct {
		subs  []*Client
		key   string
		value json.RawMessage
	}
	notifyEntries := make([]notifyEntry, 0, len(updates))
	for _, upd := range updates {
		subs := make([]*Client, 0, len(h.subs[upd.key]))
		for sub := range h.subs[upd.key] {
			subs = append(subs, sub)
		}
		notifyEntries = append(notifyEntries, notifyEntry{
			subs:  subs,
			key:   upd.key,
			value: hintMarshalList(upd.list),
		})
	}
	h.mu.Unlock()

	// Fan out SetReply to subscribers of each affected key (outside the lock).
	for _, ne := range notifyEntries {
		if len(ne.subs) == 0 {
			continue
		}
		msg := frame(map[string]any{
			"cmd":   "SetReply",
			"key":   ne.key,
			"value": ne.value,
		})
		for _, sub := range ne.subs {
			sub.enqueue(msg)
		}
	}

	// Broadcast a PrintJSON "Hint" for each new hint.
	// Reuses h.printJSON and h.broadcast from session.go.
	for _, hint := range newHints {
		h.broadcast(hintPrintJSONFrame(h, hint))
	}
}

// ---- handleUpdateHint -------------------------------------------------------

// handleUpdateHint implements the AP "UpdateHint" command (MultiServer.py ~line 2077).
//
// Parses "player" (finding player slot), "location" (int64), and "status" (int).
// Finds the matching hint in the finder's list, validates the status transition,
// updates the hint in all affected lists (finder + receiver), and fans out notifications.
//
// Validation mirrors Python:
//   - status must be a valid HintStatus value.
//   - HINT_FOUND (40) cannot be set manually.
//   - Only the receiving player may update a hint's status (permission check).
//     In this server c.slot is always the acting client; we check c.slot ==
//     hint.ReceivingPlayer (single-player receiver; group slots are not yet modelled).
//   - re_prioritize: if hint.Found and status != HINT_FOUND, status is forced to
//     HINT_FOUND (found hints can't be de-prioritised).
func (h *Hub) handleUpdateHint(c *Client, cmd map[string]json.RawMessage) {
	if h.md == nil {
		return // synthetic mode: no-op
	}

	// Parse required fields.
	var player int
	var location int64
	var statusRaw int

	if err := json.Unmarshal(cmd["player"], &player); err != nil {
		c.enqueue(frame(map[string]any{
			"cmd": "InvalidPacket", "type": "arguments",
			"text": "UpdateHint", "original_cmd": cmd,
		}))
		return
	}
	if err := json.Unmarshal(cmd["location"], &location); err != nil {
		c.enqueue(frame(map[string]any{
			"cmd": "InvalidPacket", "type": "arguments",
			"text": "UpdateHint", "original_cmd": cmd,
		}))
		return
	}
	if err := json.Unmarshal(cmd["status"], &statusRaw); err != nil {
		// status is None in Python → ignore (return silently like Python's `if status is None: return`)
		return
	}

	// Validate status value.
	if !hintStatusValid(statusRaw) {
		c.enqueue(frame(map[string]any{
			"cmd": "InvalidPacket", "type": "arguments",
			"text": "UpdateHint: Invalid Status", "original_cmd": cmd,
		}))
		return
	}
	if statusRaw == hintStatusFound {
		c.enqueue(frame(map[string]any{
			"cmd": "InvalidPacket", "type": "arguments",
			"text": `UpdateHint: Cannot manually update status to "HINT_FOUND"`, "original_cmd": cmd,
		}))
		return
	}

	team := c.team
	finderKey := hintKey(team, player)

	h.mu.Lock()
	finderList := hintLoadList(h.store[finderKey])
	// Find the hint by (player/finder, location).
	idx := -1
	for i, hint := range finderList {
		if hint.FindingPlayer == player && hint.Location == location {
			idx = i
			break
		}
	}
	if idx < 0 {
		h.mu.Unlock()
		return // hint not found — ignore safely (matches Python)
	}
	oldHint := finderList[idx]

	// Permission check: only the receiver may update.
	if c.slot != oldHint.ReceivingPlayer {
		h.mu.Unlock()
		c.enqueue(frame(map[string]any{
			"cmd": "InvalidPacket", "type": "arguments",
			"text": "UpdateHint: No Permission", "original_cmd": cmd,
		}))
		return
	}

	// re_prioritize: if found and status != HINT_FOUND, force HINT_FOUND.
	newStatus := statusRaw
	if oldHint.Found && newStatus != hintStatusFound {
		newStatus = hintStatusFound
	}
	if newStatus == oldHint.Status {
		h.mu.Unlock()
		return // no change
	}

	newHint := oldHint
	newHint.Status = newStatus

	// Update all affected lists: finder + receiver (may be same slot for local items).
	affectedSlots := map[int]struct{}{
		player:               {},
		oldHint.ReceivingPlayer: {},
	}

	type slotUpdate struct {
		key  string
		list []Hint
	}
	updates := make(map[int]slotUpdate, len(affectedSlots))
	for slot := range affectedSlots {
		key := hintKey(team, slot)
		list := hintLoadList(h.store[key])
		for i, h2 := range list {
			if hintEqual(h2, oldHint) {
				list[i].Status = newStatus
				break
			}
		}
		updates[slot] = slotUpdate{key: key, list: list}
	}
	for _, upd := range updates {
		h.store[upd.key] = hintMarshalList(upd.list)
	}

	// Snapshot subs before releasing lock.
	type notifyEntry struct {
		subs  []*Client
		key   string
		value json.RawMessage
	}
	notifyEntries := make([]notifyEntry, 0, len(updates))
	for _, upd := range updates {
		subs := make([]*Client, 0, len(h.subs[upd.key]))
		for sub := range h.subs[upd.key] {
			subs = append(subs, sub)
		}
		notifyEntries = append(notifyEntries, notifyEntry{
			subs:  subs,
			key:   upd.key,
			value: hintMarshalList(upd.list),
		})
	}
	h.mu.Unlock()

	// Fan out SetReply.
	for _, ne := range notifyEntries {
		if len(ne.subs) == 0 {
			continue
		}
		msg := frame(map[string]any{
			"cmd":   "SetReply",
			"key":   ne.key,
			"value": ne.value,
		})
		for _, sub := range ne.subs {
			sub.enqueue(msg)
		}
	}

	// Broadcast a PrintJSON "Hint" with the updated hint.
	h.broadcast(hintPrintJSONFrame(h, newHint))
}

// ---- PrintJSON helper -------------------------------------------------------

// hintPrintJSONFrame builds the wire frame for a "Hint" type PrintJSON message.
// Mirrors NetUtils.Hint.as_network_message():
//
//	{"cmd":"PrintJSON","type":"Hint","data":[...],"receiving":rp,"item":{NetworkItem},"found":bool}
//
// The data array contains text parts describing the hint in AP's format.
// We reuse h.printJSON (session.go) for the cmd/type/data envelope, and add
// the extra Hint-specific fields (receiving, item, found) via the extra map.
func hintPrintJSONFrame(h *Hub, hint Hint) []byte {
	// Simplified data parts (matching NetUtils.Hint.as_network_message structure):
	// "[Hint]: <receiving_player>'s <item> is at <location> in <finding_player>'s World. <status>"
	parts := []any{
		map[string]any{"text": "[Hint]: "},
		map[string]any{"text": fmt.Sprintf("%d", hint.ReceivingPlayer), "type": "player_id"},
		map[string]any{"text": "'s "},
		map[string]any{"text": fmt.Sprintf("%d", hint.Item), "type": "item_id", "flags": hint.ItemFlags, "player": hint.ReceivingPlayer},
		map[string]any{"text": " is at "},
		map[string]any{"text": fmt.Sprintf("%d", hint.Location), "type": "location_id", "player": hint.FindingPlayer},
		map[string]any{"text": " in "},
		map[string]any{"text": fmt.Sprintf("%d", hint.FindingPlayer), "type": "player_id"},
		map[string]any{"text": "'s World"},
		map[string]any{"text": ". "},
		map[string]any{"text": hintStatusName(hint.Status), "hint_status": hint.Status, "type": "hint_status"},
	}

	// NetworkItem embedded in the PrintJSON (item=item, location=loc, player=finder, flags=flags)
	ni := NetworkItem{
		Item:     hint.Item,
		Location: hint.Location,
		Player:   hint.FindingPlayer,
		Flags:    hint.ItemFlags,
	}

	return h.printJSON("Hint", parts, map[string]any{
		"receiving": hint.ReceivingPlayer,
		"item":      ni,
		"found":     hint.Found,
	})
}

// hintStatusName returns the human-readable status string (mirrors status_names dict in NetUtils).
func hintStatusName(status int) string {
	switch status {
	case hintStatusFound:
		return "(found)"
	case hintStatusUnspecified:
		return "(unspecified)"
	case hintStatusNoPriority:
		return "(no priority)"
	case hintStatusAvoid:
		return "(avoid)"
	case hintStatusPriority:
		return "(priority)"
	default:
		return "(unknown)"
	}
}
