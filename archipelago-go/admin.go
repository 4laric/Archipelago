// admin.go — Batch E admin/control commands for peliarch.
//
// Implements handleCommand (the "!" / "/" dispatcher wired from handleSay in
// session.go) plus three per-command handlers:
//
//	adminRelease   — route all of the issuer's own unchecked locations to their
//	                 targets (mirrors MultiServer.py release_player /
//	                 register_location_checks).
//	adminCollect   — deliver to the issuer every item from any slot whose routing
//	                 table points at the issuer (mirrors collect_player /
//	                 get_for_player logic).
//	adminRemaining — list (without delivering) the items still owed to the issuer
//	                 (mirrors get_remaining).
//
// Permission modes ("disabled", "enabled", "goal", "auto", "auto_enabled") are
// honoured for all three commands exactly as MultiServer.py _cmd_release/collect/
// remaining do.
//
// CONCURRENCY RULE: hold h.mu only for map ops; build per-target delivery lists
// under the lock, RELEASE the lock, then enqueue. Follows checksReal's exact
// discipline from main.go.
//
// INTERFACE USED (declared in main.go / session.go / multidata.go, not redefined):
//   - Hub: mu, slotToC, received, checked, md, statuses
//   - Client: slot, team, enqueue
//   - frame, ints64, NetworkItem
//   - h.printJSON, h.broadcast (session.go)
//   - md.Locations, md.AllLocs, md.Options.{Release,Collect,Remaining}Mode
package main

import (
	"fmt"
	"strings"
)

// ---- handleCommand: top-level dispatcher ------------------------------------

// handleCommand is called from handleSay when the message starts with "!" or "/".
// It strips the leading marker, splits on whitespace into command + args, and
// dispatches to one of the three admin handlers. Unknown commands reply with a
// CommandResult error. Only real-mode (h.md != nil) is meaningful; in synthetic
// mode the commands are accepted but most return "not available" messages.
func (h *Hub) handleCommand(c *Client, text string) {
	// Strip leading "!" or "/".
	if len(text) == 0 {
		return
	}
	text = strings.TrimLeft(text, "!/")
	if text == "" {
		return
	}

	parts := strings.Fields(text)
	if len(parts) == 0 {
		return
	}
	cmd := strings.ToLower(parts[0])
	// args := parts[1:]  // available for future use

	switch cmd {
	case "release", "forfeit":
		h.adminRelease(c)

	case "collect":
		h.adminCollect(c)

	case "remaining":
		h.adminRemaining(c)

	default:
		reply := h.printJSON("CommandResult",
			[]any{textPart(fmt.Sprintf("Unknown command: !%s", cmd))},
			nil,
		)
		c.enqueue(reply)
	}
}

// ---- permission helpers -----------------------------------------------------

// adminPermitted checks whether mode allows the action given the issuer's current
// game status. Mirrors the permission logic in MultiServer.py _cmd_release /
// _cmd_collect / _cmd_remaining.
//
//	"enabled"      → always allowed
//	"disabled"     → never allowed
//	"goal"         → allowed only when status == clientStatusGoal (30)
//	"auto"         → allowed only when status == clientStatusGoal
//	"auto_enabled" → always allowed (same as "enabled" from client's perspective)
//
// Returns (allowed bool, denyMsg string).
func (h *Hub) adminPermitted(c *Client, mode string, verb string) (bool, string) {
	if strings.Contains(mode, "enabled") { // "enabled" or "auto_enabled"
		return true, ""
	}
	if strings.Contains(mode, "disabled") {
		return false, fmt.Sprintf(
			"Sorry, client %s has been disabled on this server. You can ask the server admin for a /%s",
			verb, verb)
	}
	// "goal" or "auto": require CLIENT_GOAL status.
	h.mu.Lock()
	status := h.statuses[c.slot]
	h.mu.Unlock()
	if status == clientStatusGoal {
		return true, ""
	}
	return false, fmt.Sprintf(
		"Sorry, client %s requires you to have beaten the game on this server. You can ask the server admin for a /%s",
		verb, verb)
}

// adminReply sends a CommandResult PrintJSON containing text back to c only.
func (h *Hub) adminReply(c *Client, text string) {
	msg := h.printJSON("CommandResult",
		[]any{textPart(text)},
		nil,
	)
	c.enqueue(msg)
}

// ---- adminRelease -----------------------------------------------------------

// adminRelease implements "/release" (a.k.a. "/forfeit").
//
// Semantics (MultiServer.py release_player + register_location_checks):
//  1. Iterate EVERY location in md.Locations[c.slot].
//  2. For each not-yet-checked location, mark it checked in h.checked[c.slot],
//     build a NetworkItem (item=tgt.Item, location=loc, player=c.slot, flags=tgt.Flags),
//     and route it to tgt.Player.
//  3. Append each item to the target's h.received[tslot] under the lock; capture
//     the send index and the target's online client pointer.
//  4. Release the lock, then enqueue ReceivedItems to each online target.
//  5. Send a RoomUpdate with the full list of just-checked locations to c.
//  6. Broadcast a "Release" PrintJSON to all clients.
//
// Follows checksReal's exact locking pattern from main.go.
func (h *Hub) adminRelease(c *Client) {
	if h.md == nil {
		h.adminReply(c, "Release is only available in real-multidata mode.")
		return
	}

	mode := h.md.Options.ReleaseMode
	if mode == "" {
		mode = "disabled"
	}
	allowed, denyMsg := h.adminPermitted(c, mode, "release")
	if !allowed {
		h.adminReply(c, denyMsg)
		return
	}

	me := c.slot

	h.mu.Lock()
	table := h.md.Locations[me]

	checked := h.checked[me]
	if checked == nil {
		checked = make(map[int64]bool)
		h.checked[me] = checked
	}

	// Build per-target delivery list (same pattern as checksReal).
	newByTarget := map[int][]NetworkItem{}
	var justChecked []int64

	for loc, tgt := range table {
		if checked[loc] {
			continue
		}
		checked[loc] = true
		justChecked = append(justChecked, loc)
		ni := NetworkItem{Item: tgt.Item, Location: loc, Player: me, Flags: tgt.Flags}
		newByTarget[tgt.Player] = append(newByTarget[tgt.Player], ni)
	}

	// Append to received lists and capture delivery targets — all under the lock.
	type delivery struct {
		c     *Client
		index int
		items []NetworkItem
	}
	var deliveries []delivery
	for tslot, items := range newByTarget {
		old := len(h.received[tslot])
		h.received[tslot] = append(h.received[tslot], items...)
		if tc := h.slotToC[tslot]; tc != nil {
			deliveries = append(deliveries, delivery{tc, old, items})
		}
	}
	h.mu.Unlock()

	// Network writes outside the lock (the fan-out win).
	for _, d := range deliveries {
		d.c.enqueue(frame(map[string]any{
			"cmd":   "ReceivedItems",
			"index": d.index,
			"items": d.items,
		}))
	}

	// RoomUpdate so the issuer's client reflects all newly-checked locations.
	if len(justChecked) > 0 {
		c.enqueue(frame(map[string]any{
			"cmd":               "RoomUpdate",
			"checked_locations": justChecked,
		}))
	}

	// Broadcast a "Release" PrintJSON to all clients (mirrors Python's
	// broadcast_text_all with type "Release").
	name := slotName(h, me)
	msg := h.printJSON("Release",
		[]any{textPart(fmt.Sprintf("%s (Team #%d) has released all remaining items from their world.",
			name, c.team+1))},
		map[string]any{
			"team": c.team,
			"slot": me,
		},
	)
	h.broadcast(msg)

	total := len(justChecked)
	if total == 0 {
		h.adminReply(c, "No remaining locations to release.")
	} else {
		h.adminReply(c, fmt.Sprintf("Released %d location(s).", total))
	}
}

// ---- adminCollect -----------------------------------------------------------

// adminCollect implements "/collect".
//
// Semantics (MultiServer.py collect_player + get_for_player):
//
//  1. Iterate EVERY slot's location table to find entries where target==c.slot.
//  2. For each such location that is not yet checked in the FINDER's checked set,
//     mark it checked (in h.checked[finderSlot]), build a NetworkItem with
//     player=finderSlot (the source tag), and add it to c.slot's received list.
//  3. Release the lock, deliver a single ReceivedItems to c (if online).
//  4. Also send RoomUpdate to each finder whose checked set we advanced.
//  5. Broadcast a "Collect" PrintJSON to all clients.
//
// Note: Python's collect_player calls register_location_checks for each source
// slot, which routes via send_items_to. The net effect is identical to what we do
// here: the item lands in the target's received list tagged with the finder slot.
// We skip count_activity (not tracked here).
func (h *Hub) adminCollect(c *Client) {
	if h.md == nil {
		h.adminReply(c, "Collect is only available in real-multidata mode.")
		return
	}

	mode := h.md.Options.CollectMode
	if mode == "" {
		mode = "disabled"
	}
	allowed, denyMsg := h.adminPermitted(c, mode, "collect")
	if !allowed {
		h.adminReply(c, denyMsg)
		return
	}

	me := c.slot

	h.mu.Lock()

	// For each finder slot, scan its location table for locations whose target is me.
	type finderRoomUpdate struct {
		c    *Client
		locs []int64
	}
	var myNewItems []NetworkItem
	var finderUpdates []finderRoomUpdate
	startIndex := len(h.received[me])

	for finderSlot, table := range h.md.Locations {
		finderChecked := h.checked[finderSlot]
		if finderChecked == nil {
			finderChecked = make(map[int64]bool)
			h.checked[finderSlot] = finderChecked
		}

		var newlyCheckedForFinder []int64
		for loc, tgt := range table {
			if tgt.Player != me {
				continue
			}
			if finderChecked[loc] {
				continue
			}
			finderChecked[loc] = true
			newlyCheckedForFinder = append(newlyCheckedForFinder, loc)
			ni := NetworkItem{Item: tgt.Item, Location: loc, Player: finderSlot, Flags: tgt.Flags}
			myNewItems = append(myNewItems, ni)
		}

		if len(newlyCheckedForFinder) > 0 {
			// Capture the finder's online client (if any) for RoomUpdate.
			fc := h.slotToC[finderSlot]
			finderUpdates = append(finderUpdates, finderRoomUpdate{fc, newlyCheckedForFinder})
		}
	}

	// Append all collected items to the issuer's received list.
	if len(myNewItems) > 0 {
		h.received[me] = append(h.received[me], myNewItems...)
	}

	// Capture the issuer's own client pointer for ReceivedItems delivery.
	issuerC := h.slotToC[me]
	h.mu.Unlock()

	// Deliver ReceivedItems to the issuer (outside the lock).
	if len(myNewItems) > 0 && issuerC != nil {
		issuerC.enqueue(frame(map[string]any{
			"cmd":   "ReceivedItems",
			"index": startIndex,
			"items": myNewItems,
		}))
	}

	// Send RoomUpdate to each finder whose checked set advanced.
	for _, fu := range finderUpdates {
		if fu.c != nil {
			fu.c.enqueue(frame(map[string]any{
				"cmd":               "RoomUpdate",
				"checked_locations": fu.locs,
			}))
		}
	}

	// Broadcast a "Collect" PrintJSON (mirrors Python's broadcast_text_all type "Collect").
	name := slotName(h, me)
	msg := h.printJSON("Collect",
		[]any{textPart(fmt.Sprintf("%s (Team #%d) has collected their items from other worlds.",
			name, c.team+1))},
		map[string]any{
			"team": c.team,
			"slot": me,
		},
	)
	h.broadcast(msg)

	if len(myNewItems) == 0 {
		h.adminReply(c, "No remaining items to collect.")
	} else {
		h.adminReply(c, fmt.Sprintf("Collected %d item(s).", len(myNewItems)))
	}
}

// ---- adminRemaining ---------------------------------------------------------

// adminRemaining implements "/remaining".
//
// Semantics (MultiServer.py get_remaining via LocationStore.get_remaining):
//
//	For EVERY slot's location table, find locations whose target==c.slot that are
//	NOT yet checked in that slot's checked set. Collect their item IDs.
//	Reply to the issuer only (no broadcast) with a CommandResult listing the count
//	and up to 20 item IDs (to keep the message short; the Python server sends all
//	names but we lack the name tables here).
func (h *Hub) adminRemaining(c *Client) {
	if h.md == nil {
		h.adminReply(c, "Remaining is only available in real-multidata mode.")
		return
	}

	mode := h.md.Options.RemainingMode
	if mode == "" {
		mode = "disabled"
	}
	allowed, denyMsg := h.adminPermitted(c, mode, "remaining")
	if !allowed {
		h.adminReply(c, denyMsg)
		return
	}

	me := c.slot

	h.mu.Lock()
	// Collect (target_slot_for_display, item_id) pairs for unchecked locations
	// pointing at me — mirrors LocationStore.get_remaining which returns
	// [(receiving_player, item_id)] sorted.
	type remaining struct {
		sourceSlot int
		itemID     int64
	}
	var items []remaining
	for finderSlot, table := range h.md.Locations {
		finderChecked := h.checked[finderSlot]
		for loc, tgt := range table {
			if tgt.Player != me {
				continue
			}
			if finderChecked[loc] {
				continue
			}
			items = append(items, remaining{finderSlot, tgt.Item})
		}
	}
	h.mu.Unlock()

	if len(items) == 0 {
		h.adminReply(c, "No remaining items found.")
		return
	}

	// Build a short summary. Python shows all item names; we have numeric IDs only.
	const previewLimit = 20
	preview := items
	suffix := ""
	if len(items) > previewLimit {
		preview = items[:previewLimit]
		suffix = fmt.Sprintf(" ... and %d more", len(items)-previewLimit)
	}

	var sb strings.Builder
	fmt.Fprintf(&sb, "Remaining items (%d total): ", len(items))
	for i, it := range preview {
		if i > 0 {
			sb.WriteString(", ")
		}
		fmt.Fprintf(&sb, "%d", it.itemID)
	}
	sb.WriteString(suffix)

	h.adminReply(c, sb.String())
}
