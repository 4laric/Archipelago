// datastore.go — full Archipelago datastore operation set for peliarch.
//
// Implements Batch B of specs/SPEC_remaining_go_functions.md:
//   - applyOperation: all 17 operations matching MultiServer.py modify_functions
//   - handleSet: replaces the inline "case Set:" stub in main.go
//
// Concurrency model (unchanged from main.go): hold h.mu only for map reads/writes;
// snapshot subscribers under mu, release mu, then enqueue. Never hold mu across enqueue.
//
// TODO _read_* keys: hints batch (Batch D) will add computed reads for
// _read_hints_{team}_{slot}, _read_item_name_groups_{game}, etc.
package main

import (
	"encoding/json"
	"math"
)

// ---- applyOperation ---------------------------------------------------------
//
// Mirrors MultiServer.py's modify_functions exactly. cur is the current stored
// value (may be nil/null), arg is the operation's value, dflt is the Set cmd's
// "default" field (used only when cur is null — the initial seed). Returns the
// new value to store.
//
// Python uses duck-typed numerics; we coerce to float64 for all numeric ops and
// back to int64 when the result has no fractional part (matching Python's
// integer arithmetic where both inputs are int).
func applyOperation(cur json.RawMessage, operation string, arg json.RawMessage, dflt json.RawMessage) json.RawMessage {
	// When cur is JSON null or empty, seed from dflt (the cmd's "default" field).
	// This mirrors: value = ctx.stored_data.get(key, args.get("default", 0))
	if isNull(cur) {
		cur = dflt
	}

	switch operation {

	// ---- generic ops --------------------------------------------------------

	case "replace":
		// lambda old, new: new
		return arg

	case "default":
		// lambda old, new: old  (keep current; only meaningful if cur was already set)
		return cur

	// ---- numeric ops --------------------------------------------------------

	case "add":
		// operator.add — works on numbers, strings (concat), and lists (extend)
		// Try numeric first; fall back to JSON-level string concat / list extend.
		if f1, f2, ok := twoFloats(cur, arg); ok {
			return floatJSON(f1 + f2)
		}
		return addJSON(cur, arg)

	case "mul":
		f1, f2, ok := twoFloats(cur, arg)
		if !ok {
			return cur // type error → no-op (AP would raise, we silently ignore)
		}
		return floatJSON(f1 * f2)

	case "pow":
		f1, f2, ok := twoFloats(cur, arg)
		if !ok {
			return cur
		}
		return floatJSON(math.Pow(f1, f2))

	case "mod":
		f1, f2, ok := twoFloats(cur, arg)
		if !ok {
			return cur
		}
		if f2 == 0 {
			return cur // guard against zero divisor
		}
		return floatJSON(math.Mod(f1, f2))

	case "floor":
		f1, ok := oneFloat(cur)
		if !ok {
			return cur
		}
		return intJSON(int64(math.Floor(f1)))

	case "ceil":
		f1, ok := oneFloat(cur)
		if !ok {
			return cur
		}
		return intJSON(int64(math.Ceil(f1)))

	case "max":
		f1, f2, ok := twoFloats(cur, arg)
		if !ok {
			return cur
		}
		return floatJSON(math.Max(f1, f2))

	case "min":
		f1, f2, ok := twoFloats(cur, arg)
		if !ok {
			return cur
		}
		return floatJSON(math.Min(f1, f2))

	// ---- bitwise ops (integers only) ----------------------------------------

	case "xor":
		i1, i2, ok := twoInts(cur, arg)
		if !ok {
			return cur
		}
		return intJSON(i1 ^ i2)

	case "or":
		i1, i2, ok := twoInts(cur, arg)
		if !ok {
			return cur
		}
		return intJSON(i1 | i2)

	case "and":
		i1, i2, ok := twoInts(cur, arg)
		if !ok {
			return cur
		}
		return intJSON(i1 & i2)

	case "left_shift":
		i1, i2, ok := twoInts(cur, arg)
		if !ok {
			return cur
		}
		if i2 < 0 || i2 >= 64 {
			return cur
		}
		return intJSON(i1 << uint(i2))

	case "right_shift":
		i1, i2, ok := twoInts(cur, arg)
		if !ok {
			return cur
		}
		if i2 < 0 || i2 >= 64 {
			return cur
		}
		return intJSON(i1 >> uint(i2))

	// ---- list/dict ops ------------------------------------------------------

	case "remove":
		// remove_from_list: list.remove(value) — removes first occurrence, no-op if absent.
		// AP only defines this on lists.
		return removeFromList(cur, arg)

	case "pop":
		// pop_from_container: list.pop(int_index) or dict.pop(key).
		// Guard: for lists, out-of-bounds index → no-op.
		return popFromContainer(cur, arg)

	case "update":
		// update_container_unique:
		//   list: extend with entries not already present
		//   dict: dict.update(entries)
		return updateContainer(cur, arg)

	default:
		// Unknown operation → no-op (AP would raise KeyError; we don't crash).
		return cur
	}
}

// ---- handleSet --------------------------------------------------------------
//
// Replaces the inline "case Set:" block in main.go dispatch().
// Semantics match MultiServer.py lines 2164–2183 exactly.
func (h *Hub) handleSet(c *Client, cmd map[string]json.RawMessage) {
	key := str(cmd, "key")
	if key == "" {
		return // malformed — no key
	}

	// "default" seed (used when key is absent from the store)
	dflt := cmd["default"]
	if isNull(dflt) {
		dflt = json.RawMessage("0") // AP defaults to 0 when "default" not supplied
	}

	// want_reply: whether the setter itself also receives the SetReply
	var wantReply bool
	json.Unmarshal(cmd["want_reply"], &wantReply)

	// Parse operations list
	var ops []map[string]json.RawMessage
	json.Unmarshal(cmd["operations"], &ops)

	// --- critical section: read original, apply ops, write new, snapshot subs ---
	h.mu.Lock()
	original := h.store[key] // value BEFORE ops
	if isNull(original) {
		original = dflt // AP: value = stored.get(key, default); original_value = copy(value)
	}
	value := original
	for _, op := range ops {
		opName := str(op, "operation")
		opArg := op["value"]
		value = applyOperation(value, opName, opArg, dflt)
	}
	h.store[key] = value

	// Snapshot subscriber set under the lock, then release before enqueue.
	subscribers := make([]*Client, 0, len(h.subs[key]))
	for sub := range h.subs[key] {
		subscribers = append(subscribers, sub)
	}
	h.mu.Unlock()
	// -------------------------------------------------------------------------

	// Build SetReply. AP echoes the entire cmd back (with cmd changed to
	// "SetReply" and value/original_value filled in), so extra client-supplied
	// keys (e.g. "slot", custom fields) are echoed automatically. We construct
	// the reply by merging cmd keys and overriding the protocol fields.
	replyMap := make(map[string]any, len(cmd)+4)
	// Echo non-standard keys the client sent (AP behaviour: ctx.broadcast(targets, [args])
	// where args is the mutated original dict including all client keys).
	standardKeys := map[string]bool{
		"cmd": true, "key": true, "default": true,
		"want_reply": true, "operations": true,
	}
	for k, v := range cmd {
		if !standardKeys[k] {
			replyMap[k] = v
		}
	}
	replyMap["cmd"] = "SetReply"
	replyMap["key"] = key
	replyMap["value"] = value
	replyMap["original_value"] = original // nil → JSON null (correct: absent key)

	msg := frame(replyMap)

	// Fan out to subscribers (always) + setter (if want_reply).
	// Deduplicate in case the setter is also a subscriber.
	sent := make(map[*Client]bool, len(subscribers)+1)
	for _, sub := range subscribers {
		sub.enqueue(msg)
		sent[sub] = true
	}
	if wantReply && !sent[c] {
		c.enqueue(msg)
	}
}

// ---- numeric helpers --------------------------------------------------------

func isNull(r json.RawMessage) bool {
	return len(r) == 0 || string(r) == "null"
}

func oneFloat(r json.RawMessage) (float64, bool) {
	var f float64
	if err := json.Unmarshal(r, &f); err != nil {
		return 0, false
	}
	return f, true
}

func twoFloats(a, b json.RawMessage) (float64, float64, bool) {
	f1, ok1 := oneFloat(a)
	f2, ok2 := oneFloat(b)
	return f1, f2, ok1 && ok2
}

func twoInts(a, b json.RawMessage) (int64, int64, bool) {
	var i1, i2 int64
	if err := json.Unmarshal(a, &i1); err != nil {
		return 0, 0, false
	}
	if err := json.Unmarshal(b, &i2); err != nil {
		return 0, 0, false
	}
	return i1, i2, true
}

// floatJSON encodes f, but if f is an integer value, encodes it as an integer
// (e.g. 3.0 → 3, not 3e+00). Mirrors Python's int/float preservation.
func floatJSON(f float64) json.RawMessage {
	if f == math.Trunc(f) && !math.IsInf(f, 0) && !math.IsNaN(f) {
		b, _ := json.Marshal(int64(f))
		return b
	}
	b, _ := json.Marshal(f)
	return b
}

func intJSON(i int64) json.RawMessage {
	b, _ := json.Marshal(i)
	return b
}

// ---- list/dict operation helpers --------------------------------------------

// addJSON handles the "add" operation for non-numeric types (string concat, list extend).
// Python's operator.add works on strings and lists; for JSON we handle those cases.
func addJSON(cur, arg json.RawMessage) json.RawMessage {
	// Try string + string
	var s1, s2 string
	if json.Unmarshal(cur, &s1) == nil && json.Unmarshal(arg, &s2) == nil {
		b, _ := json.Marshal(s1 + s2)
		return b
	}
	// Try list + list (extend)
	var l1, l2 []json.RawMessage
	if json.Unmarshal(cur, &l1) == nil && json.Unmarshal(arg, &l2) == nil {
		combined := append(l1, l2...)
		b, _ := json.Marshal(combined)
		return b
	}
	return cur // unsupported combination → no-op
}

// removeFromList removes the first occurrence of arg from the JSON array cur.
// Matches Python: container.remove(value); ValueError is silently ignored.
func removeFromList(cur, arg json.RawMessage) json.RawMessage {
	var list []json.RawMessage
	if err := json.Unmarshal(cur, &list); err != nil {
		return cur // not a list → no-op
	}
	argStr := string(arg)
	for i, elem := range list {
		if string(elem) == argStr {
			result := append(list[:i:i], list[i+1:]...)
			b, _ := json.Marshal(result)
			return b
		}
	}
	return cur // value not found → no-op (matches ValueError catch)
}

// popFromContainer removes an index from a list or a key from a dict.
// Matches Python's pop_from_container semantics including the out-of-bounds guard.
func popFromContainer(cur, arg json.RawMessage) json.RawMessage {
	// Try list pop(int_index)
	var list []json.RawMessage
	if json.Unmarshal(cur, &list) == nil {
		var idx int64
		if err := json.Unmarshal(arg, &idx); err == nil {
			// Python guard: if len(container) <= value → return container unchanged
			if idx < 0 || int(idx) >= len(list) {
				return cur
			}
			result := append(list[:idx:idx], list[idx+1:]...)
			b, _ := json.Marshal(result)
			return b
		}
		return cur // list but non-int key → no-op
	}

	// Try dict pop(key)
	var dict map[string]json.RawMessage
	if json.Unmarshal(cur, &dict) == nil {
		var key string
		if err := json.Unmarshal(arg, &key); err != nil {
			return cur // can't decode key → no-op
		}
		if _, exists := dict[key]; !exists {
			return cur // key absent → no-op (Python guard)
		}
		delete(dict, key)
		b, _ := json.Marshal(dict)
		return b
	}

	return cur // neither list nor dict → no-op
}

// updateContainer implements update_container_unique:
//
//	list: extend with entries not already in the list (uniqueness preserved)
//	dict: dict.update(entries)
func updateContainer(cur, arg json.RawMessage) json.RawMessage {
	// Try list extend-unique
	var list []json.RawMessage
	var entries []json.RawMessage
	if json.Unmarshal(cur, &list) == nil && json.Unmarshal(arg, &entries) == nil {
		// Build set of existing elements by their JSON representation
		existing := make(map[string]bool, len(list))
		for _, e := range list {
			existing[string(e)] = true
		}
		for _, e := range entries {
			if !existing[string(e)] {
				list = append(list, e)
			}
		}
		b, _ := json.Marshal(list)
		return b
	}

	// Try dict update
	var dict map[string]json.RawMessage
	var updates map[string]json.RawMessage
	if json.Unmarshal(cur, &dict) == nil && json.Unmarshal(arg, &updates) == nil {
		for k, v := range updates {
			dict[k] = v
		}
		b, _ := json.Marshal(dict)
		return b
	}

	return cur // type mismatch → no-op
}
