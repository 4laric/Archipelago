// metadata_test.go — unit tests for metadata.go (GetDataPackage + seedReadKeys).
//
// Tests:
//   1. handleGetDataPackage with specific "games" list returns only those games.
//   2. handleGetDataPackage with absent "games" returns all games in the room.
//   3. handleGetDataPackage with an unknown game name returns an empty games map.
//   4. handleGetDataPackage with nil md returns an empty DataPackage.
//   5. seedReadKeys populates _read_item_name_groups_<game> and
//      _read_location_name_groups_<game> for every game in DataPackage.
//   6. seedReadKeys falls back to {} for a game that has no name-groups field.
package main

import (
	"encoding/json"
	"sync"
	"testing"
)

// ---- helpers -----------------------------------------------------------------

// metaFakeHub builds a minimal Hub with a crafted Multidata.DataPackage.
// pkgJSON must be a JSON object shaped like md.DataPackage:
//
//	{"<game>": {"item_name_to_id":{...},"location_name_to_id":{...},
//	            "item_name_groups":{...},"location_name_groups":{...},"checksum":"..."}}
func metaFakeHub(pkgJSON string) *Hub {
	var md *Multidata
	if pkgJSON != "" {
		md = &Multidata{
			DataPackage: json.RawMessage(pkgJSON),
			Checksums:   map[string]string{},
		}
	}
	return &Hub{
		slotToC:  make(map[int]*Client),
		store:    make(map[string]json.RawMessage),
		subs:     make(map[string]map[*Client]struct{}),
		received: make(map[int][]NetworkItem),
		checked:  make(map[int]map[int64]bool),
		statuses: make(map[int]int),
		md:       md,
	}
}

// metaFakeClient builds a client with a buffered send channel (no real conn).
func metaFakeClient() *Client {
	return &Client{
		send: make(chan []byte, 64),
		done: make(chan struct{}),
	}
}

// metaDrainOne reads the first enqueued message from c.send without blocking.
func metaDrainOne(c *Client) []byte {
	select {
	case m := <-c.send:
		return m
	default:
		return nil
	}
}

// metaUnwrapDataPackage unwraps a DataPackage frame and returns the games map
// as map[string]json.RawMessage (game -> raw JSON object).
func metaUnwrapDataPackage(t *testing.T, msg []byte) map[string]json.RawMessage {
	t.Helper()
	var cmds []map[string]json.RawMessage
	if err := json.Unmarshal(msg, &cmds); err != nil {
		t.Fatalf("unwrap DataPackage frame: unmarshal outer array: %v", err)
	}
	if len(cmds) != 1 {
		t.Fatalf("unwrap DataPackage frame: expected 1 command, got %d", len(cmds))
	}
	m := cmds[0]

	var cmdName string
	json.Unmarshal(m["cmd"], &cmdName)
	if cmdName != "DataPackage" {
		t.Fatalf("unwrap DataPackage frame: cmd = %q, want DataPackage", cmdName)
	}

	// data is a JSON object with a "games" key
	var data map[string]json.RawMessage
	if err := json.Unmarshal(m["data"], &data); err != nil {
		t.Fatalf("unwrap DataPackage frame: unmarshal data: %v", err)
	}
	gamesRaw, ok := data["games"]
	if !ok {
		t.Fatalf("unwrap DataPackage frame: missing 'games' key in data")
	}
	var games map[string]json.RawMessage
	if err := json.Unmarshal(gamesRaw, &games); err != nil {
		t.Fatalf("unwrap DataPackage frame: unmarshal games: %v", err)
	}
	return games
}

// testDataPackageJSON is a minimal two-game DataPackage for all tests.
const testDataPackageJSON = `{
	"Clique": {
		"item_name_to_id": {"Button": 1},
		"location_name_to_id": {"Clique Room": 1},
		"item_name_groups": {"Everything": ["Button"]},
		"location_name_groups": {"All": ["Clique Room"]},
		"checksum": "abc123"
	},
	"ChecksFinder": {
		"item_name_to_id": {"Checks Found 1": 101},
		"location_name_to_id": {"Checks 1": 201},
		"item_name_groups": {},
		"location_name_groups": {},
		"checksum": "def456"
	}
}`

// ---- TestHandleGetDataPackage_SpecificGames -----------------------------------

func TestHandleGetDataPackage_SpecificGames(t *testing.T) {
	h := metaFakeHub(testDataPackageJSON)
	c := metaFakeClient()
	h.mu = sync.Mutex{}

	cmd := map[string]json.RawMessage{
		"cmd":   json.RawMessage(`"GetDataPackage"`),
		"games": json.RawMessage(`["Clique"]`),
	}
	h.handleGetDataPackage(c, cmd)

	msg := metaDrainOne(c)
	if msg == nil {
		t.Fatal("handleGetDataPackage sent no message")
	}
	games := metaUnwrapDataPackage(t, msg)

	if len(games) != 1 {
		t.Fatalf("expected 1 game in response, got %d: %v", len(games), keys(games))
	}
	if _, ok := games["Clique"]; !ok {
		t.Error("expected Clique in response, not found")
	}
	if _, ok := games["ChecksFinder"]; ok {
		t.Error("ChecksFinder should not be in response when not requested")
	}

	// Verify checksum is preserved in the returned package
	var pkg map[string]json.RawMessage
	json.Unmarshal(games["Clique"], &pkg)
	var checksum string
	json.Unmarshal(pkg["checksum"], &checksum)
	if checksum != "abc123" {
		t.Errorf("checksum = %q, want abc123", checksum)
	}
}

// ---- TestHandleGetDataPackage_AllGames ---------------------------------------

func TestHandleGetDataPackage_AllGames(t *testing.T) {
	h := metaFakeHub(testDataPackageJSON)
	c := metaFakeClient()
	h.mu = sync.Mutex{}

	// No "games" key => all games
	cmd := map[string]json.RawMessage{
		"cmd": json.RawMessage(`"GetDataPackage"`),
	}
	h.handleGetDataPackage(c, cmd)

	msg := metaDrainOne(c)
	if msg == nil {
		t.Fatal("handleGetDataPackage sent no message")
	}
	games := metaUnwrapDataPackage(t, msg)

	if len(games) != 2 {
		t.Fatalf("expected 2 games in response, got %d: %v", len(games), keys(games))
	}
	for _, g := range []string{"Clique", "ChecksFinder"} {
		if _, ok := games[g]; !ok {
			t.Errorf("expected game %q in all-games response", g)
		}
	}
}

// ---- TestHandleGetDataPackage_NullGames --------------------------------------

func TestHandleGetDataPackage_NullGames(t *testing.T) {
	h := metaFakeHub(testDataPackageJSON)
	c := metaFakeClient()
	h.mu = sync.Mutex{}

	// "games": null => treat as all games (json.Unmarshal into []string yields nil)
	cmd := map[string]json.RawMessage{
		"cmd":   json.RawMessage(`"GetDataPackage"`),
		"games": json.RawMessage(`null`),
	}
	h.handleGetDataPackage(c, cmd)

	msg := metaDrainOne(c)
	if msg == nil {
		t.Fatal("handleGetDataPackage sent no message")
	}
	games := metaUnwrapDataPackage(t, msg)
	if len(games) != 2 {
		t.Errorf("null games: expected all 2 games, got %d", len(games))
	}
}

// ---- TestHandleGetDataPackage_UnknownGame ------------------------------------

func TestHandleGetDataPackage_UnknownGame(t *testing.T) {
	h := metaFakeHub(testDataPackageJSON)
	c := metaFakeClient()
	h.mu = sync.Mutex{}

	cmd := map[string]json.RawMessage{
		"cmd":   json.RawMessage(`"GetDataPackage"`),
		"games": json.RawMessage(`["UnknownGame"]`),
	}
	h.handleGetDataPackage(c, cmd)

	msg := metaDrainOne(c)
	if msg == nil {
		t.Fatal("handleGetDataPackage sent no message")
	}
	games := metaUnwrapDataPackage(t, msg)
	if len(games) != 0 {
		t.Errorf("unknown game: expected empty games map, got %v", keys(games))
	}
}

// ---- TestHandleGetDataPackage_NilMd ------------------------------------------

func TestHandleGetDataPackage_NilMd(t *testing.T) {
	h := metaFakeHub("") // nil md
	c := metaFakeClient()
	h.mu = sync.Mutex{}

	cmd := map[string]json.RawMessage{
		"cmd": json.RawMessage(`"GetDataPackage"`),
	}
	h.handleGetDataPackage(c, cmd)

	msg := metaDrainOne(c)
	if msg == nil {
		t.Fatal("handleGetDataPackage sent no message for nil md")
	}
	games := metaUnwrapDataPackage(t, msg)
	if len(games) != 0 {
		t.Errorf("nil md: expected empty games map, got %v", keys(games))
	}
}

// ---- TestSeedReadKeys_PopulatesNameGroups ------------------------------------

func TestSeedReadKeys_PopulatesNameGroups(t *testing.T) {
	h := metaFakeHub(testDataPackageJSON)
	h.mu = sync.Mutex{}

	h.seedReadKeys()

	h.mu.Lock()
	defer h.mu.Unlock()

	// Clique item_name_groups should be present and non-empty
	cliqueItems, ok := h.store["_read_item_name_groups_Clique"]
	if !ok {
		t.Error("missing store key _read_item_name_groups_Clique")
	} else {
		var groups map[string]json.RawMessage
		if err := json.Unmarshal(cliqueItems, &groups); err != nil {
			t.Errorf("_read_item_name_groups_Clique is not a valid JSON object: %v", err)
		} else if len(groups) == 0 {
			t.Error("_read_item_name_groups_Clique should not be empty for Clique")
		}
	}

	// Clique location_name_groups should be present
	cliqueLocs, ok := h.store["_read_location_name_groups_Clique"]
	if !ok {
		t.Error("missing store key _read_location_name_groups_Clique")
	} else {
		var groups map[string]json.RawMessage
		if err := json.Unmarshal(cliqueLocs, &groups); err != nil {
			t.Errorf("_read_location_name_groups_Clique is not valid JSON: %v", err)
		}
	}

	// ChecksFinder item_name_groups present (even if empty {})
	cfItems, ok := h.store["_read_item_name_groups_ChecksFinder"]
	if !ok {
		t.Error("missing store key _read_item_name_groups_ChecksFinder")
	} else {
		if string(cfItems) != "{}" && string(cfItems) != "null" {
			// May be {}, that's fine; just must be valid JSON
			var m map[string]json.RawMessage
			if err := json.Unmarshal(cfItems, &m); err != nil {
				t.Errorf("_read_item_name_groups_ChecksFinder invalid JSON: %v", err)
			}
		}
	}

	// ChecksFinder location_name_groups present
	if _, ok := h.store["_read_location_name_groups_ChecksFinder"]; !ok {
		t.Error("missing store key _read_location_name_groups_ChecksFinder")
	}
}

// ---- TestSeedReadKeys_AbsentFieldFallsBackToEmpty ----------------------------

// A game package that has no item_name_groups or location_name_groups fields.
const testDataPackageNoGroups = `{
	"MinimalGame": {
		"item_name_to_id": {"Sword": 1},
		"location_name_to_id": {"Cave": 1},
		"checksum": "minimal"
	}
}`

func TestSeedReadKeys_AbsentFieldFallsBackToEmpty(t *testing.T) {
	h := metaFakeHub(testDataPackageNoGroups)
	h.mu = sync.Mutex{}

	h.seedReadKeys()

	h.mu.Lock()
	defer h.mu.Unlock()

	for _, k := range []string{
		"_read_item_name_groups_MinimalGame",
		"_read_location_name_groups_MinimalGame",
	} {
		v, ok := h.store[k]
		if !ok {
			t.Errorf("missing store key %q", k)
			continue
		}
		// Should fall back to the empty object {}
		if string(v) != "{}" {
			t.Errorf("store[%q] = %s, want {}", k, v)
		}
	}
}

// ---- TestSeedReadKeys_NilMd --------------------------------------------------

func TestSeedReadKeys_NilMd(t *testing.T) {
	h := metaFakeHub("")
	h.mu = sync.Mutex{}

	// Must not panic; store should remain empty.
	h.seedReadKeys()

	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.store) != 0 {
		t.Errorf("nil md: expected empty store, got %d entries", len(h.store))
	}
}

// ---- helper: keys ------------------------------------------------------------

// keys returns the sorted keys of a map for readable error messages.
func keys[K comparable, V any](m map[K]V) []K {
	out := make([]K, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
