// save_test.go — round-trip tests for Hub.saveTo / Hub.loadFrom.
package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// buildTestHub returns a Hub with known received/checked/statuses/store contents
// so we can assert an exact round-trip after save + load.
func buildTestHub(t *testing.T) *Hub {
	t.Helper()
	h := newHub(nil) // synthetic mode; md == nil

	h.mu.Lock()
	defer h.mu.Unlock()

	// received: two slots, a few items each
	h.received[1] = []NetworkItem{
		{Item: 1001, Location: 2001, Player: 2, Flags: 1},
		{Item: 1002, Location: 2002, Player: 2, Flags: 0},
	}
	h.received[2] = []NetworkItem{
		{Item: 2001, Location: 1001, Player: 1, Flags: 3},
	}

	// checked: slot 1 has two checked locations
	h.checked[1] = map[int64]bool{
		5000001: true,
		5000002: true,
	}
	// slot 3 has one checked location
	h.checked[3] = map[int64]bool{
		9999999: true,
	}

	// statuses: slots 1 and 2
	h.statuses[1] = 10 // ClientStatus.CLIENT_GOAL (example)
	h.statuses[2] = 5

	// store: a counter and a list
	h.store["_read_hints_0_1"] = json.RawMessage(`[{"item":1001,"location":2001,"player":2}]`)
	h.store["counter"] = json.RawMessage(`42`)
	h.store["flag"] = json.RawMessage(`true`)

	return h
}

func TestSaveLoadRoundTrip(t *testing.T) {
	src := buildTestHub(t)

	// Write to a temp file.
	dir := t.TempDir()
	path := filepath.Join(dir, "test.archipelago-save")

	if err := src.saveTo(path); err != nil {
		t.Fatalf("saveTo: %v", err)
	}

	// Verify the file was created.
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("save file not created: %v", err)
	}

	// Load into a fresh Hub.
	dst := newHub(nil)
	if err := dst.loadFrom(path); err != nil {
		t.Fatalf("loadFrom: %v", err)
	}

	// --- assert received ---
	dst.mu.Lock()
	defer dst.mu.Unlock()

	if len(dst.received) != len(src.received) {
		t.Fatalf("received: got %d slot(s), want %d", len(dst.received), len(src.received))
	}
	for slot, want := range src.received {
		got, ok := dst.received[slot]
		if !ok {
			t.Errorf("received: slot %d missing", slot)
			continue
		}
		if len(got) != len(want) {
			t.Errorf("received[%d]: len %d != %d", slot, len(got), len(want))
			continue
		}
		for i, w := range want {
			g := got[i]
			if g.Item != w.Item || g.Location != w.Location || g.Player != w.Player || g.Flags != w.Flags {
				t.Errorf("received[%d][%d]: got %+v, want %+v", slot, i, g, w)
			}
		}
	}

	// --- assert checked ---
	if len(dst.checked) != len(src.checked) {
		t.Fatalf("checked: got %d slot(s), want %d", len(dst.checked), len(src.checked))
	}
	for slot, want := range src.checked {
		got, ok := dst.checked[slot]
		if !ok {
			t.Errorf("checked: slot %d missing", slot)
			continue
		}
		if len(got) != len(want) {
			t.Errorf("checked[%d]: len %d != %d", slot, len(got), len(want))
		}
		for loc := range want {
			if !got[loc] {
				t.Errorf("checked[%d]: location %d missing", slot, loc)
			}
		}
		for loc := range got {
			if !want[loc] {
				t.Errorf("checked[%d]: unexpected location %d", slot, loc)
			}
		}
	}

	// --- assert statuses ---
	if len(dst.statuses) != len(src.statuses) {
		t.Fatalf("statuses: got %d, want %d", len(dst.statuses), len(src.statuses))
	}
	for slot, want := range src.statuses {
		if got := dst.statuses[slot]; got != want {
			t.Errorf("statuses[%d]: got %d, want %d", slot, got, want)
		}
	}

	// --- assert store ---
	if len(dst.store) != len(src.store) {
		t.Fatalf("store: got %d key(s), want %d", len(dst.store), len(src.store))
	}
	for k, want := range src.store {
		got, ok := dst.store[k]
		if !ok {
			t.Errorf("store: key %q missing", k)
			continue
		}
		if string(got) != string(want) {
			t.Errorf("store[%q]: got %s, want %s", k, got, want)
		}
	}
}

// TestLoadFromMissingFile verifies that a missing save file is treated as a
// fresh start (no error, Hub state untouched).
func TestLoadFromMissingFile(t *testing.T) {
	h := newHub(nil)
	dir := t.TempDir()
	path := filepath.Join(dir, "nonexistent.save")

	if err := h.loadFrom(path); err != nil {
		t.Fatalf("loadFrom missing file should return nil, got: %v", err)
	}

	// Hub should still have empty maps.
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.received) != 0 {
		t.Errorf("expected empty received after fresh load, got %d entries", len(h.received))
	}
	if len(h.store) != 0 {
		t.Errorf("expected empty store after fresh load, got %d entries", len(h.store))
	}
}

// TestSaveAtomicity verifies the save does not leave a tmp file behind on success.
func TestSaveAtomicity(t *testing.T) {
	h := buildTestHub(t)
	dir := t.TempDir()
	path := filepath.Join(dir, "atomic.save")

	if err := h.saveTo(path); err != nil {
		t.Fatalf("saveTo: %v", err)
	}

	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		name := e.Name()
		if name != filepath.Base(path) {
			t.Errorf("unexpected leftover file in save dir: %s", name)
		}
	}
}

// TestSeedMismatchWarning ensures loadFrom does not error on a seed mismatch
// (it only logs a warning).  We verify by loading into a Hub that has a
// different md.SeedName.
func TestSeedMismatchWarning(t *testing.T) {
	// Build a Hub as if it has a seed "SeedA".
	srcMD := &Multidata{SeedName: "SeedA"}
	src := newHub(srcMD)
	src.mu.Lock()
	src.received[1] = []NetworkItem{{Item: 42, Location: -2, Player: 0, Flags: 0}}
	src.mu.Unlock()

	dir := t.TempDir()
	path := filepath.Join(dir, "seed.save")
	if err := src.saveTo(path); err != nil {
		t.Fatalf("saveTo: %v", err)
	}

	// Load into a Hub with a different seed — should succeed (only warn).
	dstMD := &Multidata{SeedName: "SeedB"}
	dst := newHub(dstMD)
	if err := dst.loadFrom(path); err != nil {
		t.Fatalf("loadFrom should succeed on seed mismatch, got: %v", err)
	}

	dst.mu.Lock()
	defer dst.mu.Unlock()
	if len(dst.received[1]) != 1 {
		t.Errorf("expected 1 received item after mismatch-load, got %d", len(dst.received[1]))
	}
}
