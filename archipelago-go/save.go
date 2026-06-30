// save.go — durable snapshot / restore for peliarch Hub state.
//
// Implements Batch F of specs/SPEC_remaining_go_functions.md: save/resume so the
// server survives restarts without losing per-slot progress (received items, checked
// locations, client statuses, and arbitrary datastore keys).
//
// Save format: a single flat JSON object written atomically via a tmp-file + rename.
// The format is intentionally separate from the .apsave / .archipelago pickle format
// used by the Python MultiServer; it only needs to round-trip the four live maps.
//
// Concurrency: h.mu is held only while copying the snapshot structs out; all file I/O
// (marshal + write + rename) happens AFTER the lock is released so the hot path is
// never blocked on disk.
package main

import (
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"time"
)

// saveItem is the internal serialization form for a NetworkItem.  We deliberately
// do NOT use NetworkItem.MarshalJSON (which injects "class":"NetworkItem") because
// we want a compact, stable round-trip format that doesn't depend on wire protocol
// class tags.
type saveItem struct {
	Item     int64 `json:"item"`
	Location int64 `json:"location"`
	Player   int   `json:"player"`
	Flags    int   `json:"flags"`
}

func saveItemFromNetwork(n NetworkItem) saveItem {
	return saveItem{Item: n.Item, Location: n.Location, Player: n.Player, Flags: n.Flags}
}

func (s saveItem) toNetwork() NetworkItem {
	return NetworkItem{Item: s.Item, Location: s.Location, Player: s.Player, Flags: s.Flags}
}

// saveState is the complete serializable snapshot.  int map keys are stored with
// string keys in JSON (Go's encoding/json requires this for map[int]…).
type saveState struct {
	// Seed is a sanity-check field: the md.SeedName at save time.  On load we warn
	// (but still restore) if the running server's seed doesn't match.
	Seed string `json:"seed"`

	// Received: per target slot → ordered slice of received items.
	Received map[int][]saveItem `json:"received"`

	// Checked: per slot → flat list of checked location IDs (stored as a slice to
	// avoid the nested-map JSON verbosity; converted back to map[int64]bool on load).
	Checked map[int][]int64 `json:"checked"`

	// Statuses: per slot → client status code (ClientStatus int).
	Statuses map[int]int `json:"statuses"`

	// Store: the full datastore key → raw JSON value map (already JSON-serialisable).
	Store map[string]json.RawMessage `json:"store"`
}

// saveTo snapshots the Hub under h.mu, releases the lock, then writes the snapshot
// atomically (tmp file + rename) so a crash mid-write never leaves a partial file.
func (h *Hub) saveTo(path string) error {
	// --- snapshot under the lock (must be fast: only copies, no I/O) ---
	h.mu.Lock()

	seedName := ""
	if h.md != nil {
		seedName = h.md.SeedName
	}

	received := make(map[int][]saveItem, len(h.received))
	for slot, items := range h.received {
		si := make([]saveItem, len(items))
		for i, ni := range items {
			si[i] = saveItemFromNetwork(ni)
		}
		received[slot] = si
	}

	checked := make(map[int][]int64, len(h.checked))
	for slot, locs := range h.checked {
		flat := make([]int64, 0, len(locs))
		for loc := range locs {
			flat = append(flat, loc)
		}
		checked[slot] = flat
	}

	statuses := make(map[int]int, len(h.statuses))
	for slot, st := range h.statuses {
		statuses[slot] = st
	}

	store := make(map[string]json.RawMessage, len(h.store))
	for k, v := range h.store {
		cp := make(json.RawMessage, len(v))
		copy(cp, v)
		store[k] = cp
	}

	h.mu.Unlock()
	// --- lock released; all file I/O below runs outside the critical section ---

	snap := saveState{
		Seed:     seedName,
		Received: received,
		Checked:  checked,
		Statuses: statuses,
		Store:    store,
	}

	data, err := json.Marshal(snap)
	if err != nil {
		return err
	}

	// Atomic write: write to a sibling tmp file, then rename.
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".archipelago-save-tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()

	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpName)
		return err
	}
	if err := os.Rename(tmpName, path); err != nil {
		os.Remove(tmpName)
		return err
	}
	return nil
}

// loadFrom reads a snapshot from path and restores it into the Hub.
// If path does not exist the function returns nil (fresh-start is normal).
// If the stored seed doesn't match the running md.SeedName a warning is logged but
// loading proceeds — the operator may have replaced the multidata intentionally.
func (h *Hub) loadFrom(path string) error {
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil // fresh start; no save file yet
	}
	if err != nil {
		return err
	}

	var snap saveState
	if err := json.Unmarshal(data, &snap); err != nil {
		return err
	}

	// Seed mismatch warning (non-fatal).
	if h.md != nil && snap.Seed != "" && snap.Seed != h.md.SeedName {
		log.Printf("save: WARNING seed mismatch: save file has %q but running seed is %q; loading anyway",
			snap.Seed, h.md.SeedName)
	}

	// --- restore under the lock ---
	h.mu.Lock()
	defer h.mu.Unlock()

	// received
	h.received = make(map[int][]NetworkItem, len(snap.Received))
	for slot, items := range snap.Received {
		ni := make([]NetworkItem, len(items))
		for i, si := range items {
			ni[i] = si.toNetwork()
		}
		h.received[slot] = ni
	}

	// checked (flat slice → map[int64]bool)
	h.checked = make(map[int]map[int64]bool, len(snap.Checked))
	for slot, locs := range snap.Checked {
		m := make(map[int64]bool, len(locs))
		for _, loc := range locs {
			m[loc] = true
		}
		h.checked[slot] = m
	}

	// statuses
	h.statuses = make(map[int]int, len(snap.Statuses))
	for slot, st := range snap.Statuses {
		h.statuses[slot] = st
	}

	// store
	h.store = make(map[string]json.RawMessage, len(snap.Store))
	for k, v := range snap.Store {
		cp := make(json.RawMessage, len(v))
		copy(cp, v)
		h.store[k] = cp
	}

	return nil
}

// autoSave runs a periodic save loop in a goroutine until the process exits.
// Call it as: go h.autoSave(path, 30*time.Second)
func (h *Hub) autoSave(path string, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for range ticker.C {
		if err := h.saveTo(path); err != nil {
			log.Printf("save: periodic save to %q failed: %v", path, err)
		} else {
			log.Printf("save: wrote snapshot to %q", path)
		}
	}
}
