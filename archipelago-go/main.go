// peliarch — a protocol-compatible Archipelago MultiServer in Go.
//
// Two modes:
//   * SYNTHETIC (default): fabricated slots/locations, like mock_server.py — isolates the
//     server ARCHITECTURE (Go goroutines, no GIL, non-blocking fanout) for the load harness.
//   * REAL (--multidata <bundle.json>): routes an actual room exported by dump_multidata.py,
//     with auth/version gating and table-lookup cross-slot routing. See
//     specs/SPEC_remaining_go_functions.md and PROTOCOL_SURFACE.md.
//
// Command handlers live across several files in this package (datastore.go, session.go,
// bounce.go, metadata.go, hints.go, admin.go, save.go); dispatch() below routes to them.
// Each client gets its own writer goroutine + buffered send channel, so a broadcast is
// O(subscribers) channel pushes and the network writes happen in parallel — the head-of-line
// blocking that walled Python at 250.
//
// Build:  go mod tidy && go build -o peliarch .
// Run:    ./peliarch --host 0.0.0.0 --port 38281                         # synthetic
//         ./peliarch --port 38281 --multidata room.apgo.json --save room.gosave
package main

import (
	"encoding/json"
	"flag"
	"log"
	"net/http"
	"sort"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

var upgrader = websocket.Upgrader{
	ReadBufferSize:  1 << 16,
	WriteBufferSize: 1 << 16,
	CheckOrigin:     func(r *http.Request) bool { return true },
}

var locsPerSlot = 50 // synthetic locations each slot owns (set via --locs-per-slot)

// ---- Client: one connection, its own writer goroutine + buffered outbound ----

type Client struct {
	conn *websocket.Conn
	slot int
	team int
	tags []string
	send chan []byte   // buffered; writer goroutine drains it
	done chan struct{} // closed on disconnect to stop the writer
}

func (c *Client) writer() {
	for {
		select {
		case msg := <-c.send:
			if err := c.conn.WriteMessage(websocket.TextMessage, msg); err != nil {
				return
			}
		case <-c.done:
			return
		}
	}
}

// enqueue never blocks the hub: a slow client backs up in its own buffer.
func (c *Client) enqueue(msg []byte) {
	select {
	case c.send <- msg:
	default:
		// buffer full: drop oldest by draining one, then push (keeps newest)
		select {
		case <-c.send:
		default:
		}
		select {
		case c.send <- msg:
		default:
		}
	}
}

// ---- Hub: shared room state behind a short-held mutex ----

type Hub struct {
	mu       sync.Mutex
	slotToC  map[int]*Client
	store    map[string]json.RawMessage
	subs     map[string]map[*Client]struct{}
	nextSlot int

	// real-multidata mode (nil == synthetic mode, the original load-test behavior)
	md       *Multidata
	received map[int][]NetworkItem  // per target slot: accumulated received items
	checked  map[int]map[int64]bool // per slot: checked locations (dedupe)
	statuses map[int]int            // per-slot client status (ClientStatus), set by StatusUpdate
	roomInfo []byte                 // precomputed RoomInfo frame
}

func newHub(md *Multidata) *Hub {
	h := &Hub{
		slotToC:  make(map[int]*Client),
		store:    make(map[string]json.RawMessage),
		subs:     make(map[string]map[*Client]struct{}),
		nextSlot: 1,
		md:       md,
		received: make(map[int][]NetworkItem),
		checked:  make(map[int]map[int64]bool),
		statuses: make(map[int]int),
	}
	h.buildRoomInfo()
	h.seedReadKeys() // populate _read_*_name_groups_* from the datapackage (no-op if md==nil)
	return h
}

// buildRoomInfo precomputes the pre-Connect handshake frame. Real mode advertises the
// room's games + datapackage checksums (so a client knows whether to fetch the datapackage)
// and whether a password gate is set; synthetic mode keeps the original Clique stub.
func (h *Hub) buildRoomInfo() {
	ver := map[string]any{"major": 0, "minor": 6, "build": 1, "class": "Version"}
	games := []string{"Clique"}
	checksums := map[string]any{}
	password := false
	if h.md != nil {
		games = h.md.gamesList()
		for g, cs := range h.md.Checksums {
			checksums[g] = cs
		}
		password = h.md.Password != ""
	}
	h.roomInfo = frame(map[string]any{
		"cmd":                   "RoomInfo",
		"version":               ver,
		"generator_version":     ver,
		"tags":                  []string{},
		"password":              password,
		"permissions":           map[string]any{},
		"games":                 games,
		"datapackage_checksums": checksums,
	})
}

func marshal(v any) []byte { b, _ := json.Marshal(v); return b }

// one array-of-commands frame
func frame(cmds ...any) []byte { return marshal(cmds) }

func (h *Hub) handle(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	c := &Client{conn: conn, send: make(chan []byte, 8192), done: make(chan struct{})}
	go c.writer()

	// RoomInfo first, like the real server (precomputed)
	c.enqueue(h.roomInfo)

	defer func() {
		h.mu.Lock()
		// only drop the slot mapping if THIS client still owns it (a reconnect may
		// have replaced us). Room state (received/checked) persists across disconnects.
		if c.slot != 0 && h.slotToC[c.slot] == c {
			delete(h.slotToC, c.slot)
		}
		for _, set := range h.subs {
			delete(set, c)
		}
		h.mu.Unlock()
		close(c.done)
		conn.Close()
	}()

	for {
		_, raw, err := conn.ReadMessage()
		if err != nil {
			return
		}
		var cmds []map[string]json.RawMessage
		if json.Unmarshal(raw, &cmds) != nil {
			continue
		}
		for _, cmd := range cmds {
			h.dispatch(c, cmd)
		}
	}
}

func str(m map[string]json.RawMessage, k string) string {
	var s string
	json.Unmarshal(m[k], &s)
	return s
}
func ints(m map[string]json.RawMessage, k string) []int {
	var xs []int
	json.Unmarshal(m[k], &xs)
	return xs
}
func ints64(m map[string]json.RawMessage, k string) []int64 {
	var xs []int64
	json.Unmarshal(m[k], &xs)
	return xs
}

// versionFromCmd parses Connect's version, accepting both the object form clients send
// ({"major":..,"minor":..,"build":..,"class":"Version"}) and a bare [maj,min,build] list.
func versionFromCmd(cmd map[string]json.RawMessage) Version {
	raw := cmd["version"]
	if len(raw) > 0 && raw[0] == '{' {
		var o struct {
			Major int `json:"major"`
			Minor int `json:"minor"`
			Build int `json:"build"`
		}
		if json.Unmarshal(raw, &o) == nil {
			return Version{o.Major, o.Minor, o.Build}
		}
	}
	if a := ints(map[string]json.RawMessage{"v": raw}, "v"); len(a) >= 3 {
		return Version{a[0], a[1], a[2]}
	}
	return Version{}
}

// _non_game_messages tags (MultiServer.py:954): these may connect without a game match.
var nonGameTags = map[string]bool{"HintGame": true, "Tracker": true, "TextOnly": true}

func hasNonGameTag(tags []string) bool {
	for _, t := range tags {
		if nonGameTags[t] {
			return true
		}
	}
	return false
}

// missingFor returns the slot's not-yet-checked locations (caller holds h.mu).
func (h *Hub) missingFor(slot int) []int64 {
	all := h.md.AllLocs[slot]
	checked := h.checked[slot]
	if len(checked) == 0 {
		out := make([]int64, len(all))
		copy(out, all)
		return out
	}
	out := make([]int64, 0, len(all))
	for _, l := range all {
		if !checked[l] {
			out = append(out, l)
		}
	}
	return out
}

func sortedKeys(m map[int64]bool) []int64 {
	out := make([]int64, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

func (h *Hub) dispatch(c *Client, cmd map[string]json.RawMessage) {
	switch str(cmd, "cmd") {

	case "Connect":
		if h.md != nil {
			h.connectReal(c, cmd)
		} else {
			h.connectSynthetic(c)
		}

	case "ConnectUpdate":
		h.handleConnectUpdate(c, cmd)

	case "Sync":
		h.handleSync(c, cmd)

	case "LocationChecks":
		if h.md != nil {
			h.checksReal(c, cmd)
		} else {
			h.checksSynthetic(c, cmd)
		}

	case "LocationScouts":
		if h.md != nil {
			h.scoutsReal(c, cmd)
		} else {
			locs := ints(cmd, "locations")
			info := make([]any, 0, len(locs))
			for _, l := range locs {
				info = append(info, map[string]any{"item": l, "location": l, "player": c.slot, "flags": 0})
			}
			c.enqueue(frame(map[string]any{"cmd": "LocationInfo", "locations": info}))
		}

	case "StatusUpdate":
		h.handleStatusUpdate(c, cmd)

	case "Say":
		h.handleSay(c, cmd)

	case "Bounce":
		h.handleBounce(c, cmd)

	case "GetDataPackage":
		h.handleGetDataPackage(c, cmd)

	case "CreateHints":
		h.handleCreateHints(c, cmd)

	case "UpdateHint":
		h.handleUpdateHint(c, cmd)

	case "Get":
		keys := []string{}
		json.Unmarshal(cmd["keys"], &keys)
		out := map[string]any{}
		h.mu.Lock()
		for _, k := range keys {
			if v, ok := h.store[k]; ok {
				out[k] = v
			} else {
				out[k] = nil
			}
		}
		h.mu.Unlock()
		c.enqueue(frame(map[string]any{"cmd": "Retrieved", "keys": out}))

	case "SetNotify":
		keys := []string{}
		json.Unmarshal(cmd["keys"], &keys)
		h.mu.Lock()
		for _, k := range keys {
			if h.subs[k] == nil {
				h.subs[k] = make(map[*Client]struct{})
			}
			h.subs[k][c] = struct{}{}
		}
		h.mu.Unlock()

	case "Set":
		h.handleSet(c, cmd)
	}
}

// ---- Synthetic mode (original load-test behavior, no multidata) ----

func (h *Hub) connectSynthetic(c *Client) {
	h.mu.Lock()
	c.slot = h.nextSlot
	h.nextSlot++
	h.slotToC[c.slot] = c
	base := c.slot * 100000
	h.mu.Unlock()
	missing := make([]int, locsPerSlot)
	for i := range missing {
		missing[i] = base + i
	}
	c.enqueue(frame(map[string]any{
		"cmd": "Connected", "team": 0, "slot": c.slot, "players": []any{},
		"missing_locations": missing, "checked_locations": []int{},
		"slot_data": map[string]any{}, "slot_info": map[string]any{}, "hint_points": 0,
	}))
}

func (h *Hub) checksSynthetic(c *Client, cmd map[string]json.RawMessage) {
	// route each checked location's item to a DIFFERENT slot (S -> S+1), carrying
	// its source (player=finder slot, location) so the harness pairs send/recv.
	locs := ints(cmd, "locations")
	h.mu.Lock()
	n := h.nextSlot - 1
	var targets []*Client
	for range locs {
		target := c.slot
		if n > 1 {
			target = (c.slot % n) + 1
		}
		targets = append(targets, h.slotToC[target])
	}
	h.mu.Unlock()
	for i, loc := range locs {
		t := targets[i]
		if t == nil {
			t = c
		}
		t.enqueue(frame(map[string]any{
			"cmd": "ReceivedItems", "index": 0,
			"items": []any{map[string]any{
				"item": loc, "location": loc, "player": c.slot, "flags": 0,
			}},
		}))
	}
}

// ---- Real mode (multidata loaded): auth + table-lookup routing ----

func (h *Hub) connectReal(c *Client, cmd map[string]json.RawMessage) {
	name := str(cmd, "name")
	game := str(cmd, "game")
	var password string
	json.Unmarshal(cmd["password"], &password) // JSON null -> ""
	ver := versionFromCmd(cmd)
	var ih int
	ihBad := len(cmd["items_handling"]) > 0 && json.Unmarshal(cmd["items_handling"], &ih) != nil
	var tags []string
	json.Unmarshal(cmd["tags"], &tags)

	var errs []string
	if h.md.Password != "" && password != h.md.Password {
		errs = append(errs, "InvalidPassword")
	}
	ts, ok := h.md.ConnectNames[name]
	if !ok {
		errs = append(errs, "InvalidSlot")
	} else {
		slot := ts[1]
		si := h.md.SlotInfo[slot]
		ignoreGame := game == "" && hasNonGameTag(tags)
		if !ignoreGame && game != si.Game {
			errs = append(errs, "InvalidGame")
		}
		if mv, has := h.md.MinVersions[slot]; has && !ver.GE(mv) {
			errs = append(errs, "IncompatibleVersion")
		}
		if ihBad {
			errs = append(errs, "InvalidItemsHandling")
		}
	}
	if len(errs) > 0 {
		c.enqueue(frame(map[string]any{"cmd": "ConnectionRefused", "errors": errs}))
		return
	}

	team, slot := ts[0], ts[1]
	h.mu.Lock()
	c.team, c.slot, c.tags = team, slot, tags
	h.slotToC[slot] = c
	if _, exists := h.received[slot]; !exists {
		h.received[slot] = precollectedItems(h.md.Precollected[slot]) // seed start inventory once
	}
	recv := append([]NetworkItem(nil), h.received[slot]...)
	missing := h.missingFor(slot)
	checkedList := sortedKeys(h.checked[slot])
	h.mu.Unlock()

	slotData := json.RawMessage(h.md.SlotData[slot])
	if len(slotData) == 0 {
		slotData = json.RawMessage("{}")
	}
	c.enqueue(frame(map[string]any{
		"cmd": "Connected", "team": team, "slot": slot,
		"players":           h.md.playersPackage(),
		"missing_locations": missing,
		"checked_locations": checkedList,
		"slot_info":         h.md.slotInfoPackage(),
		"slot_data":         slotData,
		"hint_points":       0,
	}))
	// initial received list (start inventory + anything routed here while offline)
	if len(recv) > 0 {
		c.enqueue(frame(map[string]any{"cmd": "ReceivedItems", "index": 0, "items": recv}))
	}
}

func (h *Hub) checksReal(c *Client, cmd map[string]json.RawMessage) {
	locs := ints64(cmd, "locations")
	me := c.slot

	h.mu.Lock()
	table := h.md.Locations[me]
	checked := h.checked[me]
	if checked == nil {
		checked = make(map[int64]bool)
		h.checked[me] = checked
	}
	// group newly-found items by destination slot
	newByTarget := map[int][]NetworkItem{}
	var justChecked []int64
	for _, loc := range locs {
		if checked[loc] {
			continue
		}
		tgt, known := table[loc]
		if !known {
			continue // not one of this slot's locations
		}
		checked[loc] = true
		justChecked = append(justChecked, loc)
		// source-tagged: player=finder slot, location=loc (so the harness pairs send/recv)
		ni := NetworkItem{Item: tgt.Item, Location: loc, Player: me, Flags: tgt.Flags}
		newByTarget[tgt.Player] = append(newByTarget[tgt.Player], ni)
	}
	// append to each target's received list under the lock, capture send index + online conn
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

	// network writes happen outside the lock, across writer goroutines (the fan-out win)
	for _, d := range deliveries {
		d.c.enqueue(frame(map[string]any{
			"cmd": "ReceivedItems", "index": d.index, "items": d.items,
		}))
	}
	// RoomUpdate so the finder's client reflects the newly checked locations
	if len(justChecked) > 0 {
		c.enqueue(frame(map[string]any{"cmd": "RoomUpdate", "checked_locations": justChecked}))
	}
}

// scoutsReal answers LocationScouts with the REAL items at the scouted locations.
func (h *Hub) scoutsReal(c *Client, cmd map[string]json.RawMessage) {
	locs := ints64(cmd, "locations")
	table := h.md.Locations[c.slot]
	info := make([]NetworkItem, 0, len(locs))
	for _, loc := range locs {
		if tgt, known := table[loc]; known {
			// LocationInfo: player is the RECEIVING player (target), location is where found
			info = append(info, NetworkItem{Item: tgt.Item, Location: loc, Player: tgt.Player, Flags: tgt.Flags})
		}
	}
	c.enqueue(frame(map[string]any{"cmd": "LocationInfo", "locations": info}))
}

func main() {
	host := flag.String("host", "0.0.0.0", "bind host")
	port := flag.String("port", "38281", "bind port")
	lps := flag.Int("locs-per-slot", 50, "synthetic locations per slot (match the room you compare against)")
	mdPath := flag.String("multidata", "", "path to a dump_multidata.py JSON bundle; enables real-room routing (default: synthetic)")
	savePath := flag.String("save", "", "path to persist room state (received/checked/store); resumes on restart")
	pwOverride := flag.String("password", "", "override the room connection password (gates Connect); empty keeps the multidata's")
	flag.Parse()
	locsPerSlot = *lps

	var md *Multidata
	if *mdPath != "" {
		var err error
		md, err = LoadMultidata(*mdPath)
		if err != nil {
			log.Fatalf("loading multidata %q: %v", *mdPath, err)
		}
		nLoc := 0
		for _, t := range md.Locations {
			nLoc += len(t)
		}
		log.Printf("loaded multidata %q: seed=%s slots=%d locations=%d games=%v",
			*mdPath, md.SeedName, len(md.SlotInfo), nLoc, md.gamesList())
	}

	// --password overrides the multidata's embedded password (gates Connect).
	if md != nil && *pwOverride != "" {
		md.Password = *pwOverride
	}

	hub := newHub(md)

	if *savePath != "" {
		if err := hub.loadFrom(*savePath); err != nil {
			log.Fatalf("loading save file %q: %v", *savePath, err)
		}
		go hub.autoSave(*savePath, 30*time.Second)
		log.Printf("persistence enabled: %s (autosave 30s)", *savePath)
	}

	http.HandleFunc("/", hub.handle)
	addr := *host + ":" + *port
	if md != nil {
		log.Printf("peliarch listening on ws://%s (real multidata: %s)", addr, md.SeedName)
	} else {
		log.Printf("peliarch listening on ws://%s (synthetic, %d locs/slot)", addr, locsPerSlot)
	}
	srv := &http.Server{Addr: addr, ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(srv.ListenAndServe())
}
