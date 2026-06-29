// archipela-go — a protocol-compatible Archipelago MultiServer skeleton in Go.
//
// Goal: stand up the exact wire surface ap_loadtest.py exercises (Connect, Get,
// LocationChecks->ReceivedItems routing, SetNotify/Set fanout) with the concurrency
// model PROTOCOL_SURFACE.md argues for — so the SAME harness scores it and you can
// diff the curves against stock (Python) MultiServer.
//
// Like mock_server.py this uses SYNTHETIC slots/locations (no multidata): it isolates
// the SERVER ARCHITECTURE (Go goroutines, no GIL, non-blocking fanout) from generation.
// Each client gets its own writer goroutine + buffered send channel, so a broadcast is
// O(subscribers) channel pushes and the network writes happen in parallel — exactly the
// head-of-line blocking that serialized at Python's 250-slot knee.
//
// Build:  go mod tidy && go build -o archipela-go .
// Run:    ./archipela-go --host 0.0.0.0 --port 38281
// Drive:  python ap_loadtest.py --host localhost --port 38281 --slots 1000 ...
//         (no multidata needed; synthetic like the mock)
package main

import (
	"encoding/json"
	"flag"
	"log"
	"net/http"
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
	send chan []byte      // buffered; writer goroutine drains it
	done chan struct{}    // closed on disconnect to stop the writer
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
}

func newHub() *Hub {
	return &Hub{
		slotToC:  make(map[int]*Client),
		store:    make(map[string]json.RawMessage),
		subs:     make(map[string]map[*Client]struct{}),
		nextSlot: 1,
	}
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

	// RoomInfo first, like the real server
	c.enqueue(frame(map[string]any{
		"cmd":     "RoomInfo",
		"version": map[string]any{"major": 0, "minor": 6, "build": 1, "class": "Version"},
		"tags":    []string{}, "password": false, "permissions": map[string]any{},
		"games": []string{"Clique"}, "datapackage_checksums": map[string]any{},
	}))

	defer func() {
		h.mu.Lock()
		if c.slot != 0 {
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

func (h *Hub) dispatch(c *Client, cmd map[string]json.RawMessage) {
	switch str(cmd, "cmd") {

	case "Connect":
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

	case "LocationChecks":
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

	case "LocationScouts":
		locs := ints(cmd, "locations")
		info := make([]any, 0, len(locs))
		for _, l := range locs {
			info = append(info, map[string]any{"item": l, "location": l, "player": c.slot, "flags": 0})
		}
		c.enqueue(frame(map[string]any{"cmd": "LocationInfo", "locations": info}))

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
		key := str(cmd, "key")
		// value = last operation's value (the setter's send timestamp, in the harness)
		var ops []map[string]json.RawMessage
		json.Unmarshal(cmd["operations"], &ops)
		var val json.RawMessage
		if len(ops) > 0 {
			val = ops[len(ops)-1]["value"]
		}
		h.mu.Lock()
		h.store[key] = val
		// snapshot subscribers, then release the lock BEFORE enqueuing the fanout
		subscribers := make([]*Client, 0, len(h.subs[key]))
		for sub := range h.subs[key] {
			subscribers = append(subscribers, sub)
		}
		h.mu.Unlock()
		msg := frame(map[string]any{
			"cmd": "SetReply", "key": key, "value": val, "original_value": nil,
		})
		for _, sub := range subscribers { // O(subscribers) channel pushes; writes run in parallel
			sub.enqueue(msg)
		}
	}
}

func main() {
	host := flag.String("host", "0.0.0.0", "bind host")
	port := flag.String("port", "38281", "bind port")
	lps := flag.Int("locs-per-slot", 50, "synthetic locations per slot (match the room you compare against)")
	flag.Parse()
	locsPerSlot = *lps

	hub := newHub()
	http.HandleFunc("/", hub.handle)
	addr := *host + ":" + *port
	log.Printf("archipela-go listening on ws://%s (synthetic, %d locs/slot)", addr, locsPerSlot)
	srv := &http.Server{Addr: addr, ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(srv.ListenAndServe())
}
