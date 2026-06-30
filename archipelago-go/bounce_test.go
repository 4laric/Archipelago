// bounce_test.go — table-driven unit tests for the Bounce/Bounced relay logic.
//
// Tests cover:
//   1. DeathLink tag match (sender receives own bounce back)
//   2. Tag filter excludes untagged clients
//   3. Game filter
//   4. Slot filter
//   5. Union semantics / no double delivery
//   6. Team isolation
//   7. Sender inclusion (Python parity — sender is NOT excluded)
//
// handleBounce integration tests use an in-process Hub with fake Clients.
package main

import (
	"encoding/json"
	"sync"
	"testing"
)

// ---- pure bounceMatches unit tests ----

func TestBounceMatches(t *testing.T) {
	dl := map[string]bool{"DeathLink": true}
	noTags := map[string]bool{}
	clique := map[string]bool{"Clique": true}
	slot3 := map[int]bool{3: true}
	noSlots := map[int]bool{}

	cases := []struct {
		name       string
		game       string
		clientTags []string
		clientSlot int
		clientTeam int
		senderTeam int
		games      map[string]bool
		tags       map[string]bool
		slots      map[int]bool
		want       bool
	}{
		{
			name:       "DeathLink tag match",
			game:       "Clique", clientTags: []string{"DeathLink"}, clientSlot: 1,
			clientTeam: 0, senderTeam: 0,
			games: noTags, tags: dl, slots: noSlots,
			want: true,
		},
		{
			name:       "tag filter excludes untagged client",
			game:       "Clique", clientTags: []string{}, clientSlot: 2,
			clientTeam: 0, senderTeam: 0,
			games: noTags, tags: dl, slots: noSlots,
			want: false,
		},
		{
			name:       "game filter matches",
			game:       "Clique", clientTags: []string{}, clientSlot: 5,
			clientTeam: 0, senderTeam: 0,
			games: clique, tags: noTags, slots: noSlots,
			want: true,
		},
		{
			name:       "game filter no match different game",
			game:       "A Link to the Past", clientTags: []string{}, clientSlot: 5,
			clientTeam: 0, senderTeam: 0,
			games: clique, tags: noTags, slots: noSlots,
			want: false,
		},
		{
			name:       "slot filter matches",
			game:       "Clique", clientTags: []string{}, clientSlot: 3,
			clientTeam: 0, senderTeam: 0,
			games: noTags, tags: noTags, slots: slot3,
			want: true,
		},
		{
			name:       "slot filter excludes different slot",
			game:       "Clique", clientTags: []string{}, clientSlot: 7,
			clientTeam: 0, senderTeam: 0,
			games: noTags, tags: noTags, slots: slot3,
			want: false,
		},
		{
			name: "union semantics: matches via tag even if slot absent",
			game: "Clique", clientTags: []string{"DeathLink"}, clientSlot: 9,
			clientTeam: 0, senderTeam: 0,
			games: noTags, tags: dl, slots: slot3,
			want: true,
		},
		{
			name: "union semantics: matches via slot even if untagged",
			game: "Clique", clientTags: []string{}, clientSlot: 3,
			clientTeam: 0, senderTeam: 0,
			games: noTags, tags: dl, slots: slot3,
			want: true,
		},
		{
			name:       "team isolation: different team rejected",
			game:       "Clique", clientTags: []string{"DeathLink"}, clientSlot: 1,
			clientTeam: 1, senderTeam: 0,
			games: noTags, tags: dl, slots: noSlots,
			want: false,
		},
		{
			name:       "sender on same team included (Python parity)",
			game:       "Clique", clientTags: []string{"DeathLink"}, clientSlot: 2,
			clientTeam: 0, senderTeam: 0,
			games: noTags, tags: dl, slots: noSlots,
			want: true,
		},
		{
			name:       "all empty filters: no match",
			game:       "Clique", clientTags: []string{"DeathLink"}, clientSlot: 1,
			clientTeam: 0, senderTeam: 0,
			games: noTags, tags: noTags, slots: noSlots,
			want: false,
		},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			got := bounceMatches(tc.game, tc.clientTags, tc.clientSlot, tc.clientTeam, tc.senderTeam, tc.games, tc.tags, tc.slots)
			if got != tc.want {
				t.Errorf("bounceMatches() = %v, want %v", got, tc.want)
			}
		})
	}
}

// ---- handleBounce integration tests ----

// makeTestHub builds a minimal Hub with a synthetic Multidata containing the
// given slot->game map. Pass nil for md to get synthetic mode.
func makeTestHub(slotGames map[int]string) *Hub {
	var md *Multidata
	if slotGames != nil {
		si := make(map[int]SlotInfo, len(slotGames))
		for slot, game := range slotGames {
			si[slot] = SlotInfo{Name: "Player", Game: game}
		}
		md = &Multidata{SlotInfo: si}
	}
	h := &Hub{
		slotToC: make(map[int]*Client),
		store:   make(map[string]json.RawMessage),
		subs:    make(map[string]map[*Client]struct{}),
		md:      md,
	}
	return h
}

// fakeClient creates a Client with a buffered send channel, no real connection.
func fakeClient(slot, team int, tags []string) *Client {
	return &Client{
		slot: slot,
		team: team,
		tags: tags,
		send: make(chan []byte, 64),
		done: make(chan struct{}),
	}
}

// received drains all enqueued messages from a client's channel (non-blocking).
func received(c *Client) [][]byte {
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

// buildBounceCmd constructs a raw Bounce command map from optional filters.
func buildBounceCmd(tags, games []string, slots []int, data map[string]any) map[string]json.RawMessage {
	cmd := map[string]json.RawMessage{
		"cmd": json.RawMessage(`"Bounce"`),
	}
	if tags != nil {
		b, _ := json.Marshal(tags)
		cmd["tags"] = b
	}
	if games != nil {
		b, _ := json.Marshal(games)
		cmd["games"] = b
	}
	if slots != nil {
		b, _ := json.Marshal(slots)
		cmd["slots"] = b
	}
	if data != nil {
		b, _ := json.Marshal(data)
		cmd["data"] = b
	}
	return cmd
}

func TestHandleBounce_DeathLinkTagMatch(t *testing.T) {
	h := makeTestHub(nil) // synthetic mode
	sender := fakeClient(1, 0, []string{"DeathLink"})
	receiver := fakeClient(2, 0, []string{"DeathLink"})
	nonDL := fakeClient(3, 0, []string{})
	h.mu.Lock()
	h.slotToC[1] = sender
	h.slotToC[2] = receiver
	h.slotToC[3] = nonDL
	h.mu.Unlock()

	data := map[string]any{"time": 1234.5, "cause": "A died", "source": "A"}
	cmd := buildBounceCmd([]string{"DeathLink"}, nil, nil, data)
	h.handleBounce(sender, cmd)

	// sender and receiver both tagged DeathLink — both should receive Bounced
	senderMsgs := received(sender)
	receiverMsgs := received(receiver)
	nonDLMsgs := received(nonDL)

	if len(senderMsgs) != 1 {
		t.Errorf("sender: want 1 Bounced, got %d", len(senderMsgs))
	}
	if len(receiverMsgs) != 1 {
		t.Errorf("receiver: want 1 Bounced, got %d", len(receiverMsgs))
	}
	if len(nonDLMsgs) != 0 {
		t.Errorf("non-DeathLink client: want 0, got %d", len(nonDLMsgs))
	}
}

func TestHandleBounce_SenderInclusion(t *testing.T) {
	// Python includes the sender — verify Go matches.
	h := makeTestHub(nil)
	sender := fakeClient(1, 0, []string{"DeathLink"})
	h.mu.Lock()
	h.slotToC[1] = sender
	h.mu.Unlock()

	cmd := buildBounceCmd([]string{"DeathLink"}, nil, nil, nil)
	h.handleBounce(sender, cmd)

	msgs := received(sender)
	if len(msgs) != 1 {
		t.Errorf("sender should receive its own Bounced; got %d messages", len(msgs))
	}
}

func TestHandleBounce_GameFilter(t *testing.T) {
	h := makeTestHub(map[int]string{1: "Clique", 2: "A Link to the Past"})
	sender := fakeClient(1, 0, []string{})
	clique := fakeClient(1, 0, []string{}) // same slot as sender for simplicity
	other := fakeClient(2, 0, []string{})
	h.mu.Lock()
	h.slotToC[1] = sender
	h.slotToC[2] = other
	h.mu.Unlock()
	_ = clique

	cmd := buildBounceCmd(nil, []string{"Clique"}, nil, nil)
	h.handleBounce(sender, cmd)

	senderMsgs := received(sender)
	otherMsgs := received(other)

	if len(senderMsgs) != 1 {
		t.Errorf("Clique sender: want 1, got %d", len(senderMsgs))
	}
	if len(otherMsgs) != 0 {
		t.Errorf("ALttP client: want 0 (game filter), got %d", len(otherMsgs))
	}
}

func TestHandleBounce_SlotFilter(t *testing.T) {
	h := makeTestHub(nil)
	sender := fakeClient(1, 0, []string{})
	target := fakeClient(3, 0, []string{})
	bystander := fakeClient(4, 0, []string{})
	h.mu.Lock()
	h.slotToC[1] = sender
	h.slotToC[3] = target
	h.slotToC[4] = bystander
	h.mu.Unlock()

	cmd := buildBounceCmd(nil, nil, []int{3}, nil)
	h.handleBounce(sender, cmd)

	if len(received(target)) != 1 {
		t.Errorf("slot 3 target: want 1 Bounced")
	}
	if len(received(bystander)) != 0 {
		t.Errorf("slot 4 bystander: want 0 (slot filter)")
	}
	if len(received(sender)) != 0 {
		// sender is slot 1, not in slots:[3] and no tag/game match
		t.Errorf("sender slot 1: want 0 (not in slot filter)")
	}
}

func TestHandleBounce_UnionSemanticsNoDoubleDelivery(t *testing.T) {
	// A client that matches BOTH tag AND slot should receive exactly one message.
	h := makeTestHub(nil)
	sender := fakeClient(1, 0, []string{})
	dual := fakeClient(5, 0, []string{"DeathLink"}) // matches tag AND slot
	h.mu.Lock()
	h.slotToC[1] = sender
	h.slotToC[5] = dual
	h.mu.Unlock()

	// Bounce with both tags:["DeathLink"] and slots:[5]
	cmd := buildBounceCmd([]string{"DeathLink"}, nil, []int{5}, nil)
	h.handleBounce(sender, cmd)

	msgs := received(dual)
	if len(msgs) != 1 {
		t.Errorf("dual-match client: want exactly 1 Bounced, got %d (no double delivery)", len(msgs))
	}
}

func TestHandleBounce_TeamIsolation(t *testing.T) {
	h := makeTestHub(nil)
	sender := fakeClient(1, 0, []string{"DeathLink"})
	sameTeam := fakeClient(2, 0, []string{"DeathLink"})
	otherTeam := fakeClient(3, 1, []string{"DeathLink"}) // team 1
	h.mu.Lock()
	h.slotToC[1] = sender
	h.slotToC[2] = sameTeam
	h.slotToC[3] = otherTeam
	h.mu.Unlock()

	cmd := buildBounceCmd([]string{"DeathLink"}, nil, nil, nil)
	h.handleBounce(sender, cmd)

	if len(received(sameTeam)) != 1 {
		t.Errorf("same-team DeathLink: want 1 Bounced")
	}
	if len(received(otherTeam)) != 0 {
		t.Errorf("other-team DeathLink: want 0 (team isolation)")
	}
}

func TestHandleBounce_VerbatimPayload(t *testing.T) {
	// The entire Bounce payload (including nested data) must survive byte-for-byte
	// as a Bounced, with only "cmd" changed.
	h := makeTestHub(nil)
	sender := fakeClient(1, 0, []string{"DeathLink"})
	h.mu.Lock()
	h.slotToC[1] = sender
	h.mu.Unlock()

	data := map[string]any{
		"time":  1700000000.0,
		"cause": "fell into the void",
		"source": "Alice",
	}
	cmd := buildBounceCmd([]string{"DeathLink"}, nil, nil, data)
	h.handleBounce(sender, cmd)

	msgs := received(sender)
	if len(msgs) != 1 {
		t.Fatalf("want 1 Bounced, got %d", len(msgs))
	}

	// msgs[0] is a JSON array of commands (frame); unwrap it.
	var cmds []map[string]json.RawMessage
	if err := json.Unmarshal(msgs[0], &cmds); err != nil || len(cmds) == 0 {
		t.Fatalf("could not parse Bounced frame: %v", err)
	}
	bounced := cmds[0]

	// cmd must be "Bounced"
	var cmdName string
	json.Unmarshal(bounced["cmd"], &cmdName)
	if cmdName != "Bounced" {
		t.Errorf("cmd: want Bounced, got %q", cmdName)
	}

	// data must round-trip intact
	var gotData map[string]any
	if err := json.Unmarshal(bounced["data"], &gotData); err != nil {
		t.Fatalf("data field missing or invalid: %v", err)
	}
	if gotData["source"] != "Alice" {
		t.Errorf("data.source: want Alice, got %v", gotData["source"])
	}
	if gotData["cause"] != "fell into the void" {
		t.Errorf("data.cause: want 'fell into the void', got %v", gotData["cause"])
	}

	// tags key must be preserved
	var gotTags []string
	json.Unmarshal(bounced["tags"], &gotTags)
	if len(gotTags) != 1 || gotTags[0] != "DeathLink" {
		t.Errorf("tags: want [DeathLink], got %v", gotTags)
	}
}

func TestHandleBounce_ConcurrentSafety(t *testing.T) {
	// Smoke test: concurrent handleBounce calls must not panic or deadlock.
	h := makeTestHub(nil)
	const N = 20
	clients := make([]*Client, N)
	h.mu.Lock()
	for i := range clients {
		clients[i] = fakeClient(i+1, 0, []string{"DeathLink"})
		h.slotToC[i+1] = clients[i]
	}
	h.mu.Unlock()

	var wg sync.WaitGroup
	for _, c := range clients {
		c := c
		wg.Add(1)
		go func() {
			defer wg.Done()
			cmd := buildBounceCmd([]string{"DeathLink"}, nil, nil, nil)
			h.handleBounce(c, cmd)
		}()
	}
	wg.Wait()
	// If we reach here without panic/deadlock, the test passes.
}
