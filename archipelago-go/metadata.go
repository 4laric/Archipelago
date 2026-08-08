// metadata.go — Batch D static-metadata commands for peliarch.
//
// Implements:
//   - handleGetDataPackage: responds to "GetDataPackage" with a DataPackage frame
//     whose shape matches MultiServer.py lines 1930–1949 exactly.
//   - seedReadKeys: populates h.store with the computed "_read_item_name_groups_<game>"
//     and "_read_location_name_groups_<game>" keys so the existing Get/SetNotify path
//     serves them (mirrors MultiServer.py read_data["item_name_groups_<game>"], line 573).
//
// CONCURRENCY RULE (same as all other files): hold h.mu only for map ops; snapshot
// under mu, release, then enqueue. Never hold mu across enqueue calls.
//
// Called from dispatch() via: case "GetDataPackage": h.handleGetDataPackage(c, cmd)
// Called from newHub()      via: h.seedReadKeys()   (after md is set and store is made)
package main

import (
	"encoding/json"
)

// metaParseDataPackage parses md.DataPackage into a map keyed by game name.
// md.DataPackage is a JSON object:
//
//	{"<game>": {"item_name_to_id":{...},"location_name_to_id":{...},
//	            "item_name_groups":{...},"location_name_groups":{...},"checksum":"..."}}
//
// Returns nil if md is nil or DataPackage is empty/null.
func metaParseDataPackage(md *Multidata) map[string]json.RawMessage {
	if md == nil || isNull(md.DataPackage) {
		return nil
	}
	var pkgs map[string]json.RawMessage
	if err := json.Unmarshal(md.DataPackage, &pkgs); err != nil {
		return nil
	}
	return pkgs
}

// metaGameField extracts a single JSON field from a per-game package object.
// Returns an empty JSON object ({}) when the game package or field is absent.
func metaGameField(gameRaw json.RawMessage, field string) json.RawMessage {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(gameRaw, &obj); err != nil {
		return json.RawMessage("{}")
	}
	if v, ok := obj[field]; ok {
		return v
	}
	return json.RawMessage("{}")
}

// handleGetDataPackage handles the "GetDataPackage" command.
//
// Protocol (MultiServer.py lines 1930–1949):
//
//	GetDataPackage{"games": ["Clique", ...]}  -> DataPackage{"data":{"games":{<game>:<pkg>}}}
//	GetDataPackage{}                           -> DataPackage for ALL games in the room
//
// Each game's package is the raw JSON object from md.DataPackage (which already
// contains item_name_to_id, location_name_to_id, and checksum as MultiServer.py
// serves from gamespackage). item_name_groups / location_name_groups are also
// present in the export and are passed through as-is; real AP clients tolerate
// extra fields and the "games" key filter matches MultiServer.py's exact filter.
func (h *Hub) handleGetDataPackage(c *Client, cmd map[string]json.RawMessage) {
	// Parse "games" list; absent or null => all games.
	var requested []string
	json.Unmarshal(cmd["games"], &requested) // safe no-op on null/absent

	pkgs := metaParseDataPackage(h.md)
	// nil md or empty DataPackage: return an empty games map.
	if pkgs == nil {
		c.enqueue(frame(map[string]any{
			"cmd":  "DataPackage",
			"data": map[string]any{"games": map[string]any{}},
		}))
		return
	}

	// Build the set of requested games. Empty slice means all.
	var wantSet map[string]bool
	if len(requested) > 0 {
		wantSet = make(map[string]bool, len(requested))
		for _, g := range requested {
			wantSet[g] = true
		}
	}

	// Collect matching games as raw JSON so we can embed them without re-encoding.
	// We build {"games": {<game>: <rawJSON>}} by hand to preserve the raw JSON values.
	gamesMap := make(map[string]json.RawMessage, len(pkgs))
	for game, gameRaw := range pkgs {
		if wantSet != nil && !wantSet[game] {
			continue
		}
		gamesMap[game] = gameRaw
	}

	// Marshal the response. The outer shape matches MultiServer.py exactly:
	// [{"cmd":"DataPackage","data":{"games":{...}}}]
	gamesJSON, _ := json.Marshal(gamesMap)
	dataJSON, _ := json.Marshal(map[string]json.RawMessage{"games": gamesJSON})
	c.enqueue(frame(map[string]json.RawMessage{
		"cmd":  json.RawMessage(`"DataPackage"`),
		"data": dataJSON,
	}))
}

// seedReadKeys populates h.store with the computed name-group read keys so that
// the existing Get/SetNotify path can serve them without any special casing.
//
// Key format (mirrors MultiServer.py read_data namespace, line 573):
//   - "_read_item_name_groups_<game>"     -> item_name_groups JSON for that game
//   - "_read_location_name_groups_<game>" -> location_name_groups JSON for that game
//
// The AP Get handler strips the "_read_" prefix and looks up the remainder in
// read_data (MultiServer.py line 2158). Our Go Get handler instead looks up the
// full key in h.store directly, so we store the full prefixed key.
//
// Called once from newHub() after md is assigned and store is initialised.
func (h *Hub) seedReadKeys() {
	if h.md == nil {
		return
	}
	pkgs := metaParseDataPackage(h.md)
	if pkgs == nil {
		return
	}

	h.mu.Lock()
	for game, gameRaw := range pkgs {
		h.store["_read_item_name_groups_"+game] = metaGameField(gameRaw, "item_name_groups")
		h.store["_read_location_name_groups_"+game] = metaGameField(gameRaw, "location_name_groups")
	}
	h.mu.Unlock()
}
