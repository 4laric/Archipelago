// hints_test.go — unit tests for CreateHints / UpdateHint (hints.go).
//
// Tests run against an in-memory Hub with a minimal Multidata (no real file I/O).
// Fake clients use buffered send channels so we can inspect enqueued messages.
//
// Coverage:
//  1. CreateHints: hint lands in both finder and receiver lists with correct fields.
//  2. CreateHints: duplicate hint is not added a second time.
//  3. CreateHints: found=true when location is pre-checked.
//  4. CreateHints: SetReply is fanned to subscribers of both hint list keys.
//  5. CreateHints: PrintJSON "Hint" is broadcast to all connected clients.
//  6. UpdateHint: status is updated in both lists.
//  7. UpdateHint: no-op when status is unchanged.
//  8. UpdateHint: HINT_FOUND cannot be set manually.
//  9. UpdateHint: non-receiver cannot update.
// 10. UpdateHint: found hint is forced to HINT_FOUND on re_prioritize.
// 11. hintStatusValid covers all valid/invalid values.
// 12. hintEqual matches on identity fields, ignores status/found.
package main

import (
	"encoding/json"
	"sync"
	"testing"
)

// ---- test helpers -----------------------------------------------------------

// hintTestHub builds a Hub with a Multidata containing two slots:
//
//	slot 1: has location 100 -> item 9001 for slot 2, flags 0
//	slot 2: has location 200 -> item 8001 for slot 1, flags 0
//
// ConnectNames is populated so both slots can "connect".
func hintTestHub() *Hub {
	md := &Multidata{
		SlotInfo: map[int]SlotInfo{
			1: {Name: "Alice", Game: "Clique"},
			2: {Name: "Bob", Game: "Clique"},
		},
		Locations: map[int]map[int64]LocationTarget{
			1: {100: {Item: 9001, Player: 2, Flags: 0}},
			2: {200: {Item: 8001, Player: 1, Flags: 0}},
		},
		ConnectNames: map[string][2]int{
			"Alice": {0, 1},
			"Bob":   {0, 2},
		},
		Options: ServerOptions{HintCost: 10},
	}
	h := &Hub{
		slotToC:  make(map[int]*Client),
		store:    make(map[string]json.RawMessage),
		subs:     make(map[string]map[*Client]struct{}),
		md:       md,
		received: make(map[int][]NetworkItem),
		checked:  make(map[int]map[int64]bool),
		statuses: make(map[int]int),
	}
	return h
}

// hintFakeClient creates a Client with a buffered channel, team 0.
func hintFakeClient(slot int) *Client {
	return &Client{
		slot: slot,
		team: 0,
		tags: []string{},
		send: make(chan []byte, 64),
		done: make(chan struct{}),
	}
}

// hintDrain reads all pending messages from c.send (non-blocking).
func hintDrain(c *Client) [][]byte {
	var out [][]byte
	for {
		select {
		case msg := <-c.send:
			out = append(out, msg)
		default:
			return out
		}
	}
}

// hintCreateCmd builds a CreateHints command map.
func hintCreateCmd(locs []int64) map[string]json.RawMessage {
	locsBytes, _ := json.Marshal(locs)
	return map[string]json.RawMessage{
		"cmd":       json.RawMessage(`"CreateHints"`),
		"locations": locsBytes,
	}
}

// hintUpdateCmd builds an UpdateHint command map.
func hintUpdateCmd(player int, location int64, status int) map[string]json.RawMessage {
	playerBytes, _ := json.Marshal(player)
	locationBytes, _ := json.Marshal(location)
	statusBytes, _ := json.Marshal(status)
	return map[string]json.RawMessage{
		"cmd":      json.RawMessage(`"UpdateHint"`),
		"player":   playerBytes,
		"location": locationBytes,
		"status":   statusBytes,
	}
}

// readHintList decodes a JSON hint list from h.store for (team, slot).
func readHintList(h *Hub, team, slot int) []Hint {
	h.mu.Lock()
	raw := h.store[hintKey(team, slot)]
	h.mu.Unlock()
	return hintLoadList(raw)
}

// ---- CreateHints tests ------------------------------------------------------

func TestCreateHints_LandsInBothLists(t *testing.T) {
	h := hintTestHub()
	alice := hintFakeClient(1) // finder: slot 1
	h.mu.Lock()
	h.slotToC[1] = alice
	h.mu.Unlock()

	cmd := hintCreateCmd([]int64{100}) // location 100 belongs to slot 1
	h.handleCreateHints(alice, cmd)

	// Check finder list (slot 1).
	finderList := readHintList(h, 0, 1)
	if len(finderList) != 1 {
		t.Fatalf("finder list: want 1 hint, got %d", len(finderList))
	}
	hint := finderList[0]
	if hint.FindingPlayer != 1 {
		t.Errorf("FindingPlayer: want 1, got %d", hint.FindingPlayer)
	}
	if hint.ReceivingPlayer != 2 {
		t.Errorf("ReceivingPlayer: want 2, got %d", hint.ReceivingPlayer)
	}
	if hint.Location != 100 {
		t.Errorf("Location: want 100, got %d", hint.Location)
	}
	if hint.Item != 9001 {
		t.Errorf("Item: want 9001, got %d", hint.Item)
	}
	if hint.Found {
		t.Error("Found: want false (location not checked)")
	}
	if hint.Entrance != "" {
		t.Errorf("Entrance: want empty, got %q", hint.Entrance)
	}
	if hint.ItemFlags != 0 {
		t.Errorf("ItemFlags: want 0, got %d", hint.ItemFlags)
	}
	if hint.Status != hintStatusUnspecified {
		t.Errorf("Status: want %d (unspecified), got %d", hintStatusUnspecified, hint.Status)
	}

	// Check receiver list (slot 2).
	receiverList := readHintList(h, 0, 2)
	if len(receiverList) != 1 {
		t.Fatalf("receiver list: want 1 hint, got %d", len(receiverList))
	}
	if receiverList[0].Location != 100 {
		t.Errorf("receiver list hint location: want 100, got %d", receiverList[0].Location)
	}
}

func TestCreateHints_NoDuplicate(t *testing.T) {
	h := hintTestHub()
	alice := hintFakeClient(1)
	h.mu.Lock()
	h.slotToC[1] = alice
	h.mu.Unlock()

	cmd := hintCreateCmd([]int64{100})
	h.handleCreateHints(alice, cmd)
	h.handleCreateHints(alice, cmd) // second call — must not duplicate

	finderList := readHintList(h, 0, 1)
	if len(finderList) != 1 {
		t.Errorf("finder list after duplicate create: want 1, got %d", len(finderList))
	}
	receiverList := readHintList(h, 0, 2)
	if len(receiverList) != 1 {
		t.Errorf("receiver list after duplicate create: want 1, got %d", len(receiverList))
	}
}

func TestCreateHints_FoundWhenChecked(t *testing.T) {
	h := hintTestHub()
	alice := hintFakeClient(1)
	h.mu.Lock()
	h.slotToC[1] = alice
	// Pre-mark location 100 as checked.
	h.checked[1] = map[int64]bool{100: true}
	h.mu.Unlock()

	cmd := hintCreateCmd([]int64{100})
	h.handleCreateHints(alice, cmd)

	list := readHintList(h, 0, 1)
	if len(list) != 1 {
		t.Fatalf("want 1 hint, got %d", len(list))
	}
	if !list[0].Found {
		t.Error("Found: want true (location pre-checked)")
	}
	if list[0].Status != hintStatusFound {
		t.Errorf("Status: want %d (found), got %d", hintStatusFound, list[0].Status)
	}
}

func TestCreateHints_SetReplyToSubscribers(t *testing.T) {
	h := hintTestHub()
	alice := hintFakeClient(1) // finder
	sub := hintFakeClient(99)  // subscriber watching slot 1's hint key

	finderKey := hintKey(0, 1)
	h.mu.Lock()
	h.slotToC[1] = alice
	h.slotToC[99] = sub
	h.subs[finderKey] = map[*Client]struct{}{sub: {}}
	h.mu.Unlock()

	cmd := hintCreateCmd([]int64{100})
	h.handleCreateHints(alice, cmd)

	msgs := hintDrain(sub)
	if len(msgs) == 0 {
		t.Fatal("subscriber received no messages; want at least 1 SetReply")
	}

	// Verify the first message is a SetReply for the finder key.
	var cmds []map[string]json.RawMessage
	if err := json.Unmarshal(msgs[0], &cmds); err != nil || len(cmds) == 0 {
		t.Fatalf("could not parse subscriber message: %v", err)
	}
	var cmdName string
	json.Unmarshal(cmds[0]["cmd"], &cmdName)
	if cmdName != "SetReply" {
		t.Errorf("subscriber cmd: want SetReply, got %q", cmdName)
	}
	var key string
	json.Unmarshal(cmds[0]["key"], &key)
	if key != finderKey {
		t.Errorf("subscriber SetReply key: want %q, got %q", finderKey, key)
	}
}

func TestCreateHints_BroadcastPrintJSON(t *testing.T) {
	h := hintTestHub()
	alice := hintFakeClient(1)
	bob := hintFakeClient(2)
	h.mu.Lock()
	h.slotToC[1] = alice
	h.slotToC[2] = bob
	h.mu.Unlock()

	cmd := hintCreateCmd([]int64{100})
	h.handleCreateHints(alice, cmd)

	// Both alice and bob should receive a PrintJSON "Hint" broadcast.
	aliceMsgs := hintDrain(alice)
	bobMsgs := hintDrain(bob)

	checkHintPrintJSON := func(name string, msgs [][]byte) {
		t.Helper()
		found := false
		for _, raw := range msgs {
			var cmds []map[string]json.RawMessage
			if json.Unmarshal(raw, &cmds) != nil || len(cmds) == 0 {
				continue
			}
			var cmdName, typ string
			json.Unmarshal(cmds[0]["cmd"], &cmdName)
			json.Unmarshal(cmds[0]["type"], &typ)
			if cmdName == "PrintJSON" && typ == "Hint" {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("%s: want a PrintJSON Hint broadcast, got none among %d messages", name, len(msgs))
		}
	}
	checkHintPrintJSON("alice", aliceMsgs)
	checkHintPrintJSON("bob", bobMsgs)
}

func TestCreateHints_EmptyLocationsReturnsError(t *testing.T) {
	h := hintTestHub()
	alice := hintFakeClient(1)
	h.mu.Lock()
	h.slotToC[1] = alice
	h.mu.Unlock()

	cmd := hintCreateCmd([]int64{})
	h.handleCreateHints(alice, cmd)

	msgs := hintDrain(alice)
	if len(msgs) == 0 {
		t.Fatal("want InvalidPacket for empty locations, got no messages")
	}
	var cmds []map[string]json.RawMessage
	json.Unmarshal(msgs[0], &cmds)
	var cmdName string
	json.Unmarshal(cmds[0]["cmd"], &cmdName)
	if cmdName != "InvalidPacket" {
		t.Errorf("cmd: want InvalidPacket, got %q", cmdName)
	}
}

// ---- UpdateHint tests -------------------------------------------------------

// hintSetup is a helper that creates a hint via CreateHints and returns the hub
// with alice (finder, slot 1) and bob (receiver, slot 2) connected.
func hintSetup(t *testing.T) (*Hub, *Client, *Client) {
	t.Helper()
	h := hintTestHub()
	alice := hintFakeClient(1)
	bob := hintFakeClient(2)
	h.mu.Lock()
	h.slotToC[1] = alice
	h.slotToC[2] = bob
	h.mu.Unlock()

	h.handleCreateHints(alice, hintCreateCmd([]int64{100}))
	hintDrain(alice) // clear setup messages
	hintDrain(bob)
	return h, alice, bob
}

func TestUpdateHint_StatusUpdated(t *testing.T) {
	h, alice, bob := hintSetup(t)
	_ = alice

	// Bob (receiver, slot 2) updates the hint to HINT_PRIORITY (30).
	cmd := hintUpdateCmd(1 /*player/finder*/, 100 /*location*/, hintStatusPriority)
	h.handleUpdateHint(bob, cmd)

	// Check finder list (slot 1).
	finderList := readHintList(h, 0, 1)
	if len(finderList) != 1 {
		t.Fatalf("finder list: want 1 hint, got %d", len(finderList))
	}
	if finderList[0].Status != hintStatusPriority {
		t.Errorf("finder list status: want %d, got %d", hintStatusPriority, finderList[0].Status)
	}

	// Check receiver list (slot 2).
	receiverList := readHintList(h, 0, 2)
	if len(receiverList) != 1 {
		t.Fatalf("receiver list: want 1 hint, got %d", len(receiverList))
	}
	if receiverList[0].Status != hintStatusPriority {
		t.Errorf("receiver list status: want %d, got %d", hintStatusPriority, receiverList[0].Status)
	}
}

func TestUpdateHint_NoOpWhenUnchanged(t *testing.T) {
	h, alice, bob := hintSetup(t)
	_ = alice

	// Set to HINT_PRIORITY first.
	h.handleUpdateHint(bob, hintUpdateCmd(1, 100, hintStatusPriority))
	hintDrain(bob)
	hintDrain(alice)

	// Now "update" to the same status — should be a no-op, no broadcast.
	h.handleUpdateHint(bob, hintUpdateCmd(1, 100, hintStatusPriority))

	aliceMsgs := hintDrain(alice)
	bobMsgs := hintDrain(bob)
	if len(aliceMsgs) != 0 || len(bobMsgs) != 0 {
		t.Errorf("no-op update: want 0 messages, got alice=%d bob=%d", len(aliceMsgs), len(bobMsgs))
	}
}

func TestUpdateHint_CannotSetFound(t *testing.T) {
	h, alice, bob := hintSetup(t)
	_ = alice

	cmd := hintUpdateCmd(1, 100, hintStatusFound)
	h.handleUpdateHint(bob, cmd)

	msgs := hintDrain(bob)
	if len(msgs) == 0 {
		t.Fatal("want InvalidPacket for HINT_FOUND, got none")
	}
	var cmds []map[string]json.RawMessage
	json.Unmarshal(msgs[0], &cmds)
	var cmdName string
	json.Unmarshal(cmds[0]["cmd"], &cmdName)
	if cmdName != "InvalidPacket" {
		t.Errorf("cmd: want InvalidPacket, got %q", cmdName)
	}
}

func TestUpdateHint_NonReceiverBlocked(t *testing.T) {
	h, alice, bob := hintSetup(t)
	_ = bob

	// Alice is the finder (slot 1), NOT the receiver — alice cannot update this hint.
	cmd := hintUpdateCmd(1, 100, hintStatusPriority)
	h.handleUpdateHint(alice, cmd)

	msgs := hintDrain(alice)
	if len(msgs) == 0 {
		t.Fatal("want InvalidPacket for non-receiver, got none")
	}
	var cmds []map[string]json.RawMessage
	json.Unmarshal(msgs[0], &cmds)
	var cmdName string
	json.Unmarshal(cmds[0]["cmd"], &cmdName)
	if cmdName != "InvalidPacket" {
		t.Errorf("cmd: want InvalidPacket, got %q", cmdName)
	}
}

func TestUpdateHint_FoundHintForcedToFound(t *testing.T) {
	// Scenario: location is checked (found=true); re_prioritize must force HINT_FOUND.
	h := hintTestHub()
	alice := hintFakeClient(1)
	bob := hintFakeClient(2)
	h.mu.Lock()
	h.slotToC[1] = alice
	h.slotToC[2] = bob
	h.checked[1] = map[int64]bool{100: true} // pre-checked
	h.mu.Unlock()

	h.handleCreateHints(alice, hintCreateCmd([]int64{100}))
	hintDrain(alice)
	hintDrain(bob)

	// Bob tries to set to HINT_PRIORITY, but since found=true, status must stay HINT_FOUND.
	// re_prioritize: if found and status != HINT_FOUND → force HINT_FOUND → same as current → no-op.
	// The store should still be HINT_FOUND.
	h.handleUpdateHint(bob, hintUpdateCmd(1, 100, hintStatusPriority))

	list := readHintList(h, 0, 1)
	if len(list) == 0 {
		t.Fatal("want hint in list")
	}
	if list[0].Status != hintStatusFound {
		t.Errorf("found hint status should remain HINT_FOUND, got %d", list[0].Status)
	}
}

func TestUpdateHint_SetReplyBroadcastToSubs(t *testing.T) {
	h, alice, bob := hintSetup(t)
	sub := hintFakeClient(99)

	finderKey := hintKey(0, 1)
	h.mu.Lock()
	h.slotToC[99] = sub
	h.subs[finderKey] = map[*Client]struct{}{sub: {}}
	h.mu.Unlock()

	hintDrain(alice)
	hintDrain(bob)
	hintDrain(sub)

	h.handleUpdateHint(bob, hintUpdateCmd(1, 100, hintStatusPriority))

	msgs := hintDrain(sub)
	if len(msgs) == 0 {
		t.Fatal("subscriber: want SetReply after UpdateHint, got none")
	}
	var cmds []map[string]json.RawMessage
	json.Unmarshal(msgs[0], &cmds)
	var cmdName string
	json.Unmarshal(cmds[0]["cmd"], &cmdName)
	if cmdName != "SetReply" {
		t.Errorf("subscriber cmd: want SetReply, got %q", cmdName)
	}
}

func TestUpdateHint_MissingHintIsNoop(t *testing.T) {
	h := hintTestHub()
	bob := hintFakeClient(2)
	h.mu.Lock()
	h.slotToC[2] = bob
	h.mu.Unlock()

	// No hints exist yet; UpdateHint should silently return.
	cmd := hintUpdateCmd(1, 100, hintStatusPriority)
	h.handleUpdateHint(bob, cmd)

	msgs := hintDrain(bob)
	if len(msgs) != 0 {
		t.Errorf("missing hint: want 0 messages, got %d", len(msgs))
	}
}

// ---- Pure helper tests ------------------------------------------------------

func TestHintStatusValid(t *testing.T) {
	valid := []int{
		hintStatusUnspecified,
		hintStatusNoPriority,
		hintStatusAvoid,
		hintStatusPriority,
		hintStatusFound,
	}
	for _, s := range valid {
		if !hintStatusValid(s) {
			t.Errorf("hintStatusValid(%d): want true", s)
		}
	}
	invalid := []int{-1, 1, 5, 15, 25, 35, 41, 100}
	for _, s := range invalid {
		if hintStatusValid(s) {
			t.Errorf("hintStatusValid(%d): want false", s)
		}
	}
}

func TestHintEqual(t *testing.T) {
	base := Hint{
		ReceivingPlayer: 2,
		FindingPlayer:   1,
		Location:        100,
		Item:            9001,
		Found:           false,
		Entrance:        "",
		ItemFlags:       0,
		Status:          hintStatusUnspecified,
	}
	// Same identity, different status/found.
	changed := base
	changed.Status = hintStatusPriority
	changed.Found = true
	if !hintEqual(base, changed) {
		t.Error("hintEqual: status/found differ but identity same — want true")
	}

	// Different location.
	diff := base
	diff.Location = 999
	if hintEqual(base, diff) {
		t.Error("hintEqual: different location — want false")
	}

	// Different entrance.
	diffEnt := base
	diffEnt.Entrance = "some_entrance"
	if hintEqual(base, diffEnt) {
		t.Error("hintEqual: different entrance — want false")
	}
}

func TestHintMarshalClass(t *testing.T) {
	hint := Hint{
		ReceivingPlayer: 2,
		FindingPlayer:   1,
		Location:        100,
		Item:            9001,
		Found:           false,
		Entrance:        "",
		ItemFlags:       0,
		Status:          hintStatusUnspecified,
	}
	b, err := json.Marshal(hint)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if m["class"] != "Hint" {
		t.Errorf("class field: want \"Hint\", got %v", m["class"])
	}
	if m["receiving_player"].(float64) != 2 {
		t.Errorf("receiving_player: want 2, got %v", m["receiving_player"])
	}
	if m["finding_player"].(float64) != 1 {
		t.Errorf("finding_player: want 1, got %v", m["finding_player"])
	}
	if m["location"].(float64) != 100 {
		t.Errorf("location: want 100, got %v", m["location"])
	}
	if m["item"].(float64) != 9001 {
		t.Errorf("item: want 9001, got %v", m["item"])
	}
}

func TestHintAddUnique(t *testing.T) {
	h := Hint{ReceivingPlayer: 2, FindingPlayer: 1, Location: 100, Item: 9001}
	list := []Hint{}

	list, added := hintAddUnique(list, h)
	if !added || len(list) != 1 {
		t.Errorf("first add: want added=true, len=1; got added=%v, len=%d", added, len(list))
	}

	list, added = hintAddUnique(list, h)
	if added || len(list) != 1 {
		t.Errorf("duplicate add: want added=false, len=1; got added=%v, len=%d", added, len(list))
	}

	h2 := Hint{ReceivingPlayer: 2, FindingPlayer: 1, Location: 200, Item: 8001}
	list, added = hintAddUnique(list, h2)
	if !added || len(list) != 2 {
		t.Errorf("different hint add: want added=true, len=2; got added=%v, len=%d", added, len(list))
	}
}

func TestHintLoadListRoundtrip(t *testing.T) {
	hints := []Hint{
		{ReceivingPlayer: 2, FindingPlayer: 1, Location: 100, Item: 9001, Status: hintStatusPriority},
		{ReceivingPlayer: 1, FindingPlayer: 2, Location: 200, Item: 8001, Status: hintStatusUnspecified},
	}
	raw := hintMarshalList(hints)
	loaded := hintLoadList(raw)

	if len(loaded) != len(hints) {
		t.Fatalf("roundtrip length: want %d, got %d", len(hints), len(loaded))
	}
	for i, want := range hints {
		got := loaded[i]
		if !hintEqual(got, want) || got.Status != want.Status || got.Found != want.Found {
			t.Errorf("hint[%d]: want %+v, got %+v", i, want, got)
		}
	}
}

// TestCreateHints_ConcurrentSafety fires many concurrent CreateHints calls and
// asserts no panic/deadlock (race detector will catch data races).
func TestCreateHints_ConcurrentSafety(t *testing.T) {
	h := hintTestHub()
	alice := hintFakeClient(1)
	bob := hintFakeClient(2)
	h.mu.Lock()
	h.slotToC[1] = alice
	h.slotToC[2] = bob
	h.mu.Unlock()

	cmd := hintCreateCmd([]int64{100})
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			h.handleCreateHints(alice, cmd)
		}()
	}
	wg.Wait()

	// Regardless of concurrency, exactly one hint should be in each list.
	finderList := readHintList(h, 0, 1)
	if len(finderList) != 1 {
		t.Errorf("concurrent CreateHints: finder list should have 1 hint, got %d", len(finderList))
	}
}
