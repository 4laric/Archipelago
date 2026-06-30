// multidata.go — load a real Archipelago room from the Go-friendly JSON bundle
// produced by dump_multidata.py. This is Batch A of specs/SPEC_remaining_go_functions.md:
// it turns peliarch from a synthetic load target into a server that routes a real room.
//
// Nothing here imports Python or unpickles the multidata: dump_multidata.py exported every
// static table once (routing table, slot identity, auth gates, datapackage), exactly as
// PROTOCOL_SURFACE.md argues ("the runtime fast path needs no Python at all").
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
)

// Version is major.minor.build, compared like AP's Version tuple.
type Version struct{ Major, Minor, Build int }

// GE reports whether v >= o (the client meets the minimum).
func (v Version) GE(o Version) bool {
	if v.Major != o.Major {
		return v.Major > o.Major
	}
	if v.Minor != o.Minor {
		return v.Minor > o.Minor
	}
	return v.Build >= o.Build
}

// LocationTarget is one row of the routing table: this location yields Item for Player.
type LocationTarget struct {
	Item   int64
	Player int
	Flags  int
}

// SlotInfo mirrors AP's NetworkSlot (name, game, type, group_members).
type SlotInfo struct {
	Name         string `json:"name"`
	Game         string `json:"game"`
	Type         int    `json:"type"`
	GroupMembers []int  `json:"group_members"`
}

// ServerOptions holds the non-hot-path config used by points/admin arithmetic.
type ServerOptions struct {
	LocationCheckPoints int    `json:"location_check_points"`
	HintCost            int    `json:"hint_cost"`
	ReleaseMode         string `json:"release_mode"`
	CollectMode         string `json:"collect_mode"`
	RemainingMode       string `json:"remaining_mode"`
}

// Multidata is the fully-decoded room: immutable after load, so reads need no lock.
type Multidata struct {
	SeedName     string
	Password     string // "" == no password gate
	Games        map[int]string
	SlotInfo     map[int]SlotInfo
	ConnectNames map[string][2]int // name -> {team, slot}
	MinVersions  map[int]Version
	Locations    map[int]map[int64]LocationTarget // slot -> loc -> target
	SlotData     map[int]json.RawMessage
	Precollected map[int][]int64 // start inventory item ids per slot
	Checksums    map[string]string
	DataPackage  json.RawMessage // full export, served verbatim by GetDataPackage (Batch D)
	Options      ServerOptions

	// derived
	AllLocs map[int][]int64 // sorted location ids per slot, for Connected.missing_locations
}

// rawBundle matches dump_multidata.py's JSON exactly (all map keys are strings).
type rawBundle struct {
	SeedName     string                        `json:"seed_name"`
	Password     *string                       `json:"password"`
	Games        map[string]string             `json:"games"`
	SlotInfo     map[string]SlotInfo           `json:"slot_info"`
	ConnectNames map[string][]int              `json:"connect_names"`
	MinVersions  map[string][]int              `json:"min_client_versions"`
	Locations    map[string]map[string][]int64 `json:"locations"`
	SlotData     map[string]json.RawMessage    `json:"slot_data"`
	Precollected map[string][]int64            `json:"precollected_items"`
	Checksums    map[string]string             `json:"datapackage_checksums"`
	DataPackage  json.RawMessage               `json:"datapackage"`
	Options      ServerOptions                 `json:"server_options"`
}

func atoi(s string) int { n, _ := strconv.Atoi(s); return n }

// LoadMultidata parses the JSON bundle written by dump_multidata.py.
func LoadMultidata(path string) (*Multidata, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var rb rawBundle
	if err := json.Unmarshal(data, &rb); err != nil {
		return nil, fmt.Errorf("parse multidata bundle: %w", err)
	}

	md := &Multidata{
		SeedName:     rb.SeedName,
		Games:        make(map[int]string, len(rb.Games)),
		SlotInfo:     make(map[int]SlotInfo, len(rb.SlotInfo)),
		ConnectNames: make(map[string][2]int, len(rb.ConnectNames)),
		MinVersions:  make(map[int]Version, len(rb.MinVersions)),
		Locations:    make(map[int]map[int64]LocationTarget, len(rb.Locations)),
		SlotData:     make(map[int]json.RawMessage, len(rb.SlotData)),
		Precollected: make(map[int][]int64, len(rb.Precollected)),
		Checksums:    rb.Checksums,
		DataPackage:  rb.DataPackage,
		Options:      rb.Options,
		AllLocs:      make(map[int][]int64, len(rb.Locations)),
	}
	if rb.Password != nil {
		md.Password = *rb.Password
	}
	for k, v := range rb.Games {
		md.Games[atoi(k)] = v
	}
	for k, v := range rb.SlotInfo {
		md.SlotInfo[atoi(k)] = v
	}
	for name, ts := range rb.ConnectNames {
		if len(ts) >= 2 {
			md.ConnectNames[name] = [2]int{ts[0], ts[1]}
		}
	}
	for k, v := range rb.MinVersions {
		if len(v) >= 3 {
			md.MinVersions[atoi(k)] = Version{v[0], v[1], v[2]}
		}
	}
	for k, v := range rb.SlotData {
		md.SlotData[atoi(k)] = v
	}
	for k, v := range rb.Precollected {
		md.Precollected[atoi(k)] = v
	}
	for slotStr, table := range rb.Locations {
		slot := atoi(slotStr)
		dst := make(map[int64]LocationTarget, len(table))
		locs := make([]int64, 0, len(table))
		for locStr, t := range table {
			if len(t) < 3 {
				continue
			}
			loc, _ := strconv.ParseInt(locStr, 10, 64)
			dst[loc] = LocationTarget{Item: t[0], Player: int(t[1]), Flags: int(t[2])}
			locs = append(locs, loc)
		}
		sort.Slice(locs, func(i, j int) bool { return locs[i] < locs[j] })
		md.Locations[slot] = dst
		md.AllLocs[slot] = locs
	}
	return md, nil
}

// NetworkItem is one item delivery. MarshalJSON injects "class":"NetworkItem" so that real
// AP clients (CommonClient does NetworkItem(*item)) reconstruct it as the namedtuple — the
// load harness reads it as a plain dict, so the class tag satisfies both.
type NetworkItem struct {
	Item     int64
	Location int64
	Player   int
	Flags    int
}

func (n NetworkItem) MarshalJSON() ([]byte, error) {
	return json.Marshal(map[string]any{
		"class": "NetworkItem", "item": n.Item, "location": n.Location,
		"player": n.Player, "flags": n.Flags,
	})
}

// precollectedItems turns a slot's start-inventory item ids into the initial received list.
// AP records start inventory as NetworkItem(item, location=-2, player=0).
func precollectedItems(ids []int64) []NetworkItem {
	if len(ids) == 0 {
		return nil
	}
	out := make([]NetworkItem, 0, len(ids))
	for _, id := range ids {
		out = append(out, NetworkItem{Item: id, Location: -2, Player: 0, Flags: 0})
	}
	return out
}

// slotInfoPackage builds the Connected.slot_info map (string keys, "class":"NetworkSlot"
// values) so a real client's {int(pid): data}.update() yields NetworkSlot namedtuples.
func (md *Multidata) slotInfoPackage() map[string]any {
	out := make(map[string]any, len(md.SlotInfo))
	for slot, si := range md.SlotInfo {
		members := si.GroupMembers
		if members == nil {
			members = []int{}
		}
		out[strconv.Itoa(slot)] = map[string]any{
			"class": "NetworkSlot", "name": si.Name, "game": si.Game,
			"type": si.Type, "group_members": members,
		}
	}
	return out
}

// playersPackage builds Connected.players as NetworkPlayer(team, slot, alias, name) entries.
func (md *Multidata) playersPackage() []any {
	out := make([]any, 0, len(md.SlotInfo))
	for slot, si := range md.SlotInfo {
		out = append(out, map[string]any{
			"class": "NetworkPlayer", "team": 0, "slot": slot,
			"alias": si.Name, "name": si.Name,
		})
	}
	return out
}

// gamesList returns the distinct games in the room (for RoomInfo).
func (md *Multidata) gamesList() []string {
	seen := map[string]bool{}
	var games []string
	for _, g := range md.Games {
		if !seen[g] {
			seen[g] = true
			games = append(games, g)
		}
	}
	sort.Strings(games)
	return games
}
