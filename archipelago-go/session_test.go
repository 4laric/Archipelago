// session_test.go — unit tests for session.go helpers.
//
// Tests cover:
//   1. printJSON output shape (cmd, type, data, extra fields).
//   2. isChatCommand detection (pure helper extracted for testability).
//   3. tagSetsEqual order-insensitivity.
//   4. handleSync: empty list sends nothing; non-empty sends ReceivedItems at index 0.
//
// We do NOT start a real HTTP/WebSocket server; all tests work against an in-memory Hub
// and a fake Client whose enqueue writes to a buffered channel we inspect.
package main

import (
	"encoding/json"
	"sync"
	"testing"
)

// ---- pure helper: isChatCommand -------------------------------------------

// isChatCommand reports whether the Say text should be routed to a command handler.
// A message is a command when it starts with "!" or "/".
// This is extracted as a pure function so tests don't need a Hub.
func isChatCommand(text string) bool {
	if len(text) == 0 {
		return false
	}
	return text[0] == '!' || text[0] == '/'
}

func TestIsChatCommand(t *testing.T) {
	cases := []struct {
		text string
		want bool
	}{
		{"!help", true},
		{"!release", true},
		{"/help", true},
		{"/status", true},
		{"hello world", false},
		{"", false},
		{"normal chat", false},
		{"!admin login secret", true}, // still a command even if broadcast is skipped
		{"?question", false},          // question mark is not a command prefix
	}
	for _, tc := range cases {
		got := isChatCommand(tc.text)
		if got != tc.want {
			t.Errorf("isChatCommand(%q) = %v, want %v", tc.text, got, tc.want)
		}
	}
}

// ---- printJSON shape -------------------------------------------------------

func TestPrintJSONShape(t *testing.T) {
	h := &Hub{}

	parts := []any{textPart("hello world")}
	extra := map[string]any{"team": 0, "slot": 1, "message": "hello world"}
	raw := h.printJSON("Chat", parts, extra)

	// raw is a JSON array (frame wraps in []); unwrap it.
	var cmds []map[string]json.RawMessage
	if err := json.Unmarshal(raw, &cmds); err != nil {
		t.Fatalf("unmarshal frame: %v", err)
	}
	if len(cmds) != 1 {
		t.Fatalf("expected 1 command in frame, got %d", len(cmds))
	}
	msg := cmds[0]

	// cmd == "PrintJSON"
	var cmd string
	json.Unmarshal(msg["cmd"], &cmd)
	if cmd != "PrintJSON" {
		t.Errorf("cmd = %q, want PrintJSON", cmd)
	}

	// type == "Chat"
	var typ string
	json.Unmarshal(msg["type"], &typ)
	if typ != "Chat" {
		t.Errorf("type = %q, want Chat", typ)
	}

	// data is a list with one element containing "text"
	var data []map[string]any
	if err := json.Unmarshal(msg["data"], &data); err != nil {
		t.Fatalf("unmarshal data: %v", err)
	}
	if len(data) != 1 {
		t.Fatalf("data length = %d, want 1", len(data))
	}
	if data[0]["text"] != "hello world" {
		t.Errorf("data[0].text = %v, want 'hello world'", data[0]["text"])
	}

	// extra fields: team, slot, message
	var team float64
	json.Unmarshal(msg["team"], &team)
	if team != 0 {
		t.Errorf("team = %v, want 0", team)
	}
	var slot float64
	json.Unmarshal(msg["slot"], &slot)
	if slot != 1 {
		t.Errorf("slot = %v, want 1", slot)
	}
}

func TestPrintJSONNoExtraFields(t *testing.T) {
	h := &Hub{}
	raw := h.printJSON("Goal", []any{textPart("done")}, nil)

	var cmds []map[string]json.RawMessage
	json.Unmarshal(raw, &cmds)
	if len(cmds) != 1 {
		t.Fatalf("expected 1 command")
	}
	// Should have cmd, type, data — and NOT panic on nil extra.
	var cmd string
	json.Unmarshal(cmds[0]["cmd"], &cmd)
	if cmd != "PrintJSON" {
		t.Errorf("cmd = %q, want PrintJSON", cmd)
	}
}

// ---- tagSetsEqual ----------------------------------------------------------

func TestTagSetsEqual(t *testing.T) {
	if !tagSetsEqual([]string{"A", "B"}, []string{"B", "A"}) {
		t.Error("order-reversed slices should be equal")
	}
	if !tagSetsEqual(nil, nil) {
		t.Error("nil slices should be equal")
	}
	if !tagSetsEqual([]string{}, []string{}) {
		t.Error("empty slices should be equal")
	}
	if tagSetsEqual([]string{"A"}, []string{"B"}) {
		t.Error("different slices should not be equal")
	}
	if tagSetsEqual([]string{"A", "B"}, []string{"A"}) {
		t.Error("different lengths should not be equal")
	}
}

// ---- handleSync ------------------------------------------------------------

// sessFakeClient builds a client with a buffered send channel, no real conn.
func sessFakeClient(slot, team int) *Client {
	return &Client{
		slot: slot,
		team: team,
		send: make(chan []byte, 64),
		done: make(chan struct{}),
	}
}

// drain reads all pending messages from c.send without blocking.
func drain(c *Client) [][]byte {
	var msgs [][]byte
	for {
		select {
		case m := <-c.send:
			msgs = append(msgs, m)
		default:
			return msgs
		}
	}
}

func TestHandleSyncEmpty(t *testing.T) {
	h := &Hub{
		slotToC:  make(map[int]*Client),
		received: make(map[int][]NetworkItem),
		statuses: make(map[int]int),
	}
	c := sessFakeClient(1, 0)
	h.mu = sync.Mutex{}

	h.handleSync(c, nil)

	msgs := drain(c)
	if len(msgs) != 0 {
		t.Errorf("expected no messages for empty received list, got %d", len(msgs))
	}
}

func TestHandleSyncSendsReceivedItems(t *testing.T) {
	h := &Hub{
		slotToC:  make(map[int]*Client),
		received: make(map[int][]NetworkItem),
		statuses: make(map[int]int),
	}
	h.mu = sync.Mutex{}
	c := sessFakeClient(2, 0)

	// Pre-populate received list for slot 2.
	h.received[2] = []NetworkItem{
		{Item: 1001, Location: 500, Player: 1, Flags: 0},
		{Item: 1002, Location: 501, Player: 3, Flags: 1},
	}

	h.handleSync(c, nil)

	msgs := drain(c)
	if len(msgs) != 1 {
		t.Fatalf("expected 1 message, got %d", len(msgs))
	}

	var cmds []map[string]json.RawMessage
	if err := json.Unmarshal(msgs[0], &cmds); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(cmds) != 1 {
		t.Fatalf("expected 1 cmd in frame, got %d", len(cmds))
	}
	m := cmds[0]

	var cmd string
	json.Unmarshal(m["cmd"], &cmd)
	if cmd != "ReceivedItems" {
		t.Errorf("cmd = %q, want ReceivedItems", cmd)
	}

	var index int
	json.Unmarshal(m["index"], &index)
	if index != 0 {
		t.Errorf("index = %d, want 0", index)
	}

	// items should be an array of 2
	var items []map[string]any
	if err := json.Unmarshal(m["items"], &items); err != nil {
		t.Fatalf("unmarshal items: %v", err)
	}
	if len(items) != 2 {
		t.Errorf("items length = %d, want 2", len(items))
	}
}

// TestHandleSyncIsolation verifies that mutations to h.received after handleSync
// do NOT affect the already-enqueued message (snapshot under mu).
func TestHandleSyncIsolation(t *testing.T) {
	h := &Hub{
		slotToC:  make(map[int]*Client),
		received: make(map[int][]NetworkItem),
		statuses: make(map[int]int),
	}
	h.mu = sync.Mutex{}
	c := sessFakeClient(3, 0)
	h.received[3] = []NetworkItem{{Item: 9, Location: 1, Player: 1, Flags: 0}}

	h.handleSync(c, nil)
	// mutate after sync
	h.received[3] = append(h.received[3], NetworkItem{Item: 10, Location: 2, Player: 2, Flags: 0})

	msgs := drain(c)
	if len(msgs) != 1 {
		t.Fatalf("expected 1 message")
	}
	var cmds []map[string]json.RawMessage
	json.Unmarshal(msgs[0], &cmds)
	var items []map[string]any
	json.Unmarshal(cmds[0]["items"], &items)
	// snapshot taken before mutation: should still be length 1
	if len(items) != 1 {
		t.Errorf("snapshot leaked mutation: items length = %d, want 1", len(items))
	}
}

// ---- handleStatusUpdate ----------------------------------------------------

func TestHandleStatusUpdate(t *testing.T) {
	h := &Hub{
		slotToC:  make(map[int]*Client),
		received: make(map[int][]NetworkItem),
		statuses: make(map[int]int),
	}
	h.mu = sync.Mutex{}
	c := sessFakeClient(1, 0)

	cmd := map[string]json.RawMessage{
		"cmd":    json.RawMessage(`"StatusUpdate"`),
		"status": json.RawMessage(`20`),
	}
	h.handleStatusUpdate(c, cmd)
	if h.statuses[1] != 20 {
		t.Errorf("statuses[1] = %d, want 20", h.statuses[1])
	}
}

func TestHandleStatusUpdateGoalIsSticky(t *testing.T) {
	h := &Hub{
		slotToC:  make(map[int]*Client),
		received: make(map[int][]NetworkItem),
		statuses: make(map[int]int),
	}
	h.mu = sync.Mutex{}
	c := sessFakeClient(1, 0)

	// Set to GOAL (30)
	h.statuses[1] = clientStatusGoal

	// Try to lower it
	cmd := map[string]json.RawMessage{
		"cmd":    json.RawMessage(`"StatusUpdate"`),
		"status": json.RawMessage(`5`),
	}
	h.handleStatusUpdate(c, cmd)

	if h.statuses[1] != clientStatusGoal {
		t.Errorf("goal status was overwritten: got %d, want %d", h.statuses[1], clientStatusGoal)
	}
}
