// admin_test.go — unit tests for admin.go (Batch E: /release, /collect, /remaining).
//
// Corrected from the initial generated version: CommandResult is the *type* of a
// PrintJSON command (cmd=="PrintJSON", type=="CommandResult"), not a command name —
// so we look it up by type. /collect also correctly includes the issuer's own-world
// items destined for themselves (AP get_for_player semantics), so slot 1 collects 3.
package main

import (
	"encoding/json"
	"strings"
	"testing"
)

func admFakeClient(slot, team int) *Client {
	return &Client{slot: slot, team: team, send: make(chan []byte, 256), done: make(chan struct{})}
}

func admDrain(c *Client) []map[string]json.RawMessage {
	var out []map[string]json.RawMessage
	for {
		select {
		case msg := <-c.send:
			var cmds []map[string]json.RawMessage
			if json.Unmarshal(msg, &cmds) == nil {
				out = append(out, cmds...)
			}
		default:
			return out
		}
	}
}

// admFirstCmd finds the first command whose "cmd" field equals name.
func admFirstCmd(cmds []map[string]json.RawMessage, name string) map[string]json.RawMessage {
	for _, c := range cmds {
		var n string
		json.Unmarshal(c["cmd"], &n)
		if n == name {
			return c
		}
	}
	return nil
}

// admFirstType finds the first PrintJSON whose "type" field equals typ.
func admFirstType(cmds []map[string]json.RawMessage, typ string) map[string]json.RawMessage {
	for _, c := range cmds {
		var n, t string
		json.Unmarshal(c["cmd"], &n)
		json.Unmarshal(c["type"], &t)
		if n == "PrintJSON" && t == typ {
			return c
		}
	}
	return nil
}

func admText(cmd map[string]json.RawMessage) string {
	var data []map[string]json.RawMessage
	json.Unmarshal(cmd["data"], &data)
	if len(data) == 0 {
		return ""
	}
	var text string
	json.Unmarshal(data[0]["text"], &text)
	return text
}

// Three slots. slot 1 owns 101→item1001(slot2), 102→item1002(slot3), 103→item1003(slot1,self).
// slot 2 owns 201→item2001(slot1). slot 3 owns 301→item3001(slot1).
func admBuildHub(releaseMode, collectMode, remainingMode string) *Hub {
	md := &Multidata{
		SeedName: "test",
		SlotInfo: map[int]SlotInfo{
			1: {Name: "Alice", Game: "Clique"},
			2: {Name: "Bob", Game: "Clique"},
			3: {Name: "Carol", Game: "Clique"},
		},
		Locations: map[int]map[int64]LocationTarget{
			1: {101: {Item: 1001, Player: 2}, 102: {Item: 1002, Player: 3}, 103: {Item: 1003, Player: 1}},
			2: {201: {Item: 2001, Player: 1}},
			3: {301: {Item: 3001, Player: 1}},
		},
		AllLocs: map[int][]int64{1: {101, 102, 103}, 2: {201}, 3: {301}},
		Options: ServerOptions{ReleaseMode: releaseMode, CollectMode: collectMode, RemainingMode: remainingMode},
	}
	return &Hub{
		slotToC:  make(map[int]*Client),
		store:    make(map[string]json.RawMessage),
		subs:     make(map[string]map[*Client]struct{}),
		md:       md,
		received: make(map[int][]NetworkItem),
		checked:  make(map[int]map[int64]bool),
		statuses: make(map[int]int),
	}
}

func admRegister(h *Hub, cs ...*Client) {
	h.mu.Lock()
	for _, c := range cs {
		h.slotToC[c.slot] = c
	}
	h.mu.Unlock()
}

func TestAdminRelease_DeliversToTargets(t *testing.T) {
	h := admBuildHub("enabled", "disabled", "disabled")
	c1, c2, c3 := admFakeClient(1, 0), admFakeClient(2, 0), admFakeClient(3, 0)
	admRegister(h, c1, c2, c3)

	h.adminRelease(c1)

	if ri := admFirstCmd(admDrain(c2), "ReceivedItems"); ri == nil {
		t.Error("slot 2 should receive its item from slot 1's release")
	}
	if ri := admFirstCmd(admDrain(c3), "ReceivedItems"); ri == nil {
		t.Error("slot 3 should receive its item from slot 1's release")
	}
	if cr := admFirstType(admDrain(c1), "CommandResult"); cr == nil {
		t.Error("issuer should get a CommandResult")
	}
}

func TestAdminRelease_Disabled(t *testing.T) {
	h := admBuildHub("disabled", "disabled", "disabled")
	c1, c2 := admFakeClient(1, 0), admFakeClient(2, 0)
	admRegister(h, c1, c2)

	h.adminRelease(c1)

	cr := admFirstType(admDrain(c1), "CommandResult")
	if cr == nil || !strings.Contains(admText(cr), "disabled") {
		t.Error("disabled release should reply with a CommandResult mentioning 'disabled'")
	}
	if ri := admFirstCmd(admDrain(c2), "ReceivedItems"); ri != nil {
		t.Error("disabled release must not deliver any items")
	}
}

func TestAdminRelease_GoalModeBlockedThenAllowed(t *testing.T) {
	h := admBuildHub("goal", "disabled", "disabled")
	c1 := admFakeClient(1, 0)
	admRegister(h, c1)

	h.adminRelease(c1) // not goaled yet → blocked
	if cr := admFirstType(admDrain(c1), "CommandResult"); cr == nil {
		t.Error("goal-mode release before goal should reply with CommandResult (deny)")
	}

	h.mu.Lock()
	h.statuses[1] = clientStatusGoal
	h.mu.Unlock()
	c2 := admFakeClient(2, 0)
	admRegister(h, c2)
	h.adminRelease(c1) // now allowed
	if ri := admFirstCmd(admDrain(c2), "ReceivedItems"); ri == nil {
		t.Error("after goal, release should deliver items")
	}
}

func TestAdminCollect_IncludesSelfItem(t *testing.T) {
	h := admBuildHub("disabled", "enabled", "disabled")
	c1, c2, c3 := admFakeClient(1, 0), admFakeClient(2, 0), admFakeClient(3, 0)
	admRegister(h, c1, c2, c3)

	h.adminCollect(c1)

	msgs := admDrain(c1)
	ri := admFirstCmd(msgs, "ReceivedItems")
	if ri == nil {
		t.Fatal("issuer should receive collected items")
	}
	var items []NetworkItem
	json.Unmarshal(ri["items"], &items)
	// AP get_for_player(1): item 2001 (slot2), 3001 (slot3), AND 1003 (slot1's own loc 103). = 3
	if len(items) != 3 {
		t.Errorf("expected 3 collected items (incl. self-item 1003), got %d: %+v", len(items), items)
	}
	got := map[int64]int{}
	for _, it := range items {
		got[it.Item] = it.Player
	}
	if got[2001] != 2 || got[3001] != 3 || got[1003] != 1 {
		t.Errorf("collected items have wrong source tags: %+v", got)
	}
	if cr := admFirstType(msgs, "CommandResult"); cr == nil {
		t.Error("collect should reply with a CommandResult")
	}
}

func TestAdminRemaining_RepliesCommandResult(t *testing.T) {
	h := admBuildHub("disabled", "disabled", "enabled")
	c1 := admFakeClient(1, 0)
	admRegister(h, c1)

	h.adminRemaining(c1)

	cr := admFirstType(admDrain(c1), "CommandResult")
	if cr == nil {
		t.Fatal("/remaining should reply with a CommandResult")
	}
	if !strings.Contains(admText(cr), "Remaining") {
		t.Errorf("unexpected /remaining text: %q", admText(cr))
	}
}

func TestAdminRemaining_Disabled(t *testing.T) {
	h := admBuildHub("disabled", "disabled", "disabled")
	c1 := admFakeClient(1, 0)
	admRegister(h, c1)
	h.adminRemaining(c1)
	if cr := admFirstType(admDrain(c1), "CommandResult"); cr == nil {
		t.Error("disabled /remaining should still reply with a CommandResult (deny)")
	}
}

func TestAdminUnknownCommand(t *testing.T) {
	h := admBuildHub("enabled", "enabled", "enabled")
	c1 := admFakeClient(1, 0)
	admRegister(h, c1)

	h.handleCommand(c1, "!frobnicate")
	cr := admFirstType(admDrain(c1), "CommandResult")
	if cr == nil || !strings.Contains(strings.ToLower(admText(cr)), "unknown") {
		t.Error("unknown command should reply with a CommandResult mentioning 'unknown'")
	}
}

func TestAdminCommand_CaseInsensitive(t *testing.T) {
	h := admBuildHub("enabled", "enabled", "enabled")
	c1, c2 := admFakeClient(1, 0), admFakeClient(2, 0)
	admRegister(h, c1, c2)
	h.handleCommand(c1, "!RELEASE")
	if ri := admFirstCmd(admDrain(c2), "ReceivedItems"); ri == nil {
		t.Error("!RELEASE (uppercase) should dispatch to release")
	}
}
