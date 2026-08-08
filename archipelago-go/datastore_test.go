// datastore_test.go — table-driven tests for applyOperation against AP semantics.
//
// Golden vectors derived from MultiServer.py modify_functions and the Set handler.
// Each test case reflects the Python output for the same input.
package main

import (
	"encoding/json"
	"testing"
)

// raw converts a Go value to json.RawMessage for test inputs.
func raw(v any) json.RawMessage {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}

// rawNull is a null json.RawMessage (absent key).
var rawNull = json.RawMessage("null")

// TestApplyOperation runs every operation against AP's expected output.
func TestApplyOperation(t *testing.T) {
	tests := []struct {
		name      string
		cur       json.RawMessage
		operation string
		arg       json.RawMessage
		dflt      json.RawMessage
		want      any // will be json.Marshal'd for comparison
	}{
		// ---- replace --------------------------------------------------------
		{
			name: "replace_number",
			cur:  raw(5), operation: "replace", arg: raw(10), dflt: raw(0),
			want: 10,
		},
		{
			name: "replace_string",
			cur:  raw("hello"), operation: "replace", arg: raw("world"), dflt: raw(""),
			want: "world",
		},
		{
			name: "replace_null_cur",
			cur:  rawNull, operation: "replace", arg: raw(42), dflt: raw(0),
			want: 42,
		},

		// ---- default --------------------------------------------------------
		{
			name: "default_keeps_existing",
			cur:  raw(7), operation: "default", arg: raw(99), dflt: raw(0),
			want: 7, // old is returned unchanged
		},
		{
			name: "default_when_absent_uses_dflt_seed",
			// When cur is null, it is first replaced by dflt (the Set cmd's "default").
			// Then "default" op returns the current value (which is now dflt).
			cur:  rawNull, operation: "default", arg: raw(99), dflt: raw(100),
			want: 100,
		},

		// ---- add ------------------------------------------------------------
		{
			name: "add_integers",
			cur:  raw(3), operation: "add", arg: raw(4), dflt: raw(0),
			want: 7,
		},
		{
			name: "add_floats",
			cur:  raw(1.5), operation: "add", arg: raw(2.5), dflt: raw(0),
			want: 4, // 4.0 → integer JSON per floatJSON
		},
		{
			name: "add_strings",
			cur:  raw("foo"), operation: "add", arg: raw("bar"), dflt: raw(""),
			want: "foobar",
		},
		{
			name: "add_lists",
			cur:  raw([]int{1, 2}), operation: "add", arg: raw([]int{3, 4}), dflt: raw([]int{}),
			want: []int{1, 2, 3, 4},
		},
		{
			name: "add_absent_key_uses_default",
			// key absent → value seeded from dflt=0; 0+5=5
			cur:  rawNull, operation: "add", arg: raw(5), dflt: raw(0),
			want: 5,
		},
		// EnergyLink-style: repeated adds converge correctly (each call starts from stored value)
		{
			name: "add_accumulation_step1",
			cur:  raw(0), operation: "add", arg: raw(1000000), dflt: raw(0),
			want: 1000000,
		},
		{
			name: "add_accumulation_step2",
			cur:  raw(1000000), operation: "add", arg: raw(1000000), dflt: raw(0),
			want: 2000000,
		},

		// ---- mul ------------------------------------------------------------
		{
			name: "mul_integers",
			cur:  raw(6), operation: "mul", arg: raw(7), dflt: raw(1),
			want: 42,
		},
		{
			name: "mul_float",
			cur:  raw(2.5), operation: "mul", arg: raw(4.0), dflt: raw(1),
			want: 10, // 10.0 → 10
		},

		// ---- pow ------------------------------------------------------------
		{
			name: "pow_integers",
			cur:  raw(2), operation: "pow", arg: raw(10), dflt: raw(1),
			want: 1024,
		},
		{
			name: "pow_float",
			cur:  raw(9.0), operation: "pow", arg: raw(0.5), dflt: raw(1),
			want: 3, // sqrt(9)=3.0 → 3
		},

		// ---- mod ------------------------------------------------------------
		{
			name: "mod_integers",
			cur:  raw(17), operation: "mod", arg: raw(5), dflt: raw(0),
			want: 2,
		},
		{
			name: "mod_zero_divisor_noop",
			cur:  raw(10), operation: "mod", arg: raw(0), dflt: raw(0),
			want: 10, // guarded no-op
		},

		// ---- floor / ceil ---------------------------------------------------
		{
			name: "floor_positive",
			cur:  raw(3.7), operation: "floor", arg: rawNull, dflt: raw(0),
			want: 3,
		},
		{
			name: "floor_negative",
			cur:  raw(-2.3), operation: "floor", arg: rawNull, dflt: raw(0),
			want: -3,
		},
		{
			name: "ceil_positive",
			cur:  raw(1.1), operation: "ceil", arg: rawNull, dflt: raw(0),
			want: 2,
		},

		// ---- max / min ------------------------------------------------------
		{
			name: "max_picks_larger",
			cur:  raw(3), operation: "max", arg: raw(7), dflt: raw(0),
			want: 7,
		},
		{
			name: "max_already_larger",
			cur:  raw(10), operation: "max", arg: raw(4), dflt: raw(0),
			want: 10,
		},
		{
			name: "min_picks_smaller",
			cur:  raw(3), operation: "min", arg: raw(7), dflt: raw(0),
			want: 3,
		},

		// ---- bitwise ops ----------------------------------------------------
		{
			name: "xor_bits",
			cur:  raw(0b1010), operation: "xor", arg: raw(0b1100), dflt: raw(0),
			want: 0b0110, // 6
		},
		{
			name: "or_bits",
			cur:  raw(0b1010), operation: "or", arg: raw(0b0101), dflt: raw(0),
			want: 0b1111, // 15
		},
		{
			name: "and_bits",
			cur:  raw(0b1111), operation: "and", arg: raw(0b1010), dflt: raw(0),
			want: 0b1010, // 10
		},
		{
			name: "left_shift",
			cur:  raw(1), operation: "left_shift", arg: raw(8), dflt: raw(0),
			want: 256,
		},
		{
			name: "right_shift",
			cur:  raw(256), operation: "right_shift", arg: raw(4), dflt: raw(0),
			want: 16,
		},

		// ---- remove ---------------------------------------------------------
		{
			name: "remove_first_occurrence",
			cur:  raw([]int{1, 2, 3, 2, 4}), operation: "remove", arg: raw(2), dflt: rawNull,
			want: []int{1, 3, 2, 4},
		},
		{
			name: "remove_absent_noop",
			cur:  raw([]int{1, 2, 3}), operation: "remove", arg: raw(99), dflt: rawNull,
			want: []int{1, 2, 3},
		},

		// ---- pop ------------------------------------------------------------
		{
			name: "pop_list_valid_index",
			cur:  raw([]string{"a", "b", "c"}), operation: "pop", arg: raw(1), dflt: rawNull,
			want: []string{"a", "c"},
		},
		{
			name: "pop_list_out_of_bounds_noop",
			// Python: if len(container) <= value → return unchanged
			cur:  raw([]int{1, 2, 3}), operation: "pop", arg: raw(5), dflt: rawNull,
			want: []int{1, 2, 3},
		},
		{
			name: "pop_dict_existing_key",
			cur:  raw(map[string]int{"a": 1, "b": 2}),
			operation: "pop", arg: raw("a"), dflt: rawNull,
			want: map[string]int{"b": 2},
		},
		{
			name: "pop_dict_absent_key_noop",
			cur:  raw(map[string]int{"a": 1}), operation: "pop", arg: raw("z"), dflt: rawNull,
			want: map[string]int{"a": 1},
		},

		// ---- update ---------------------------------------------------------
		{
			name: "update_list_unique",
			// existing [1,2,3], entries [2,3,4,5] → only 4 and 5 appended
			cur:  raw([]int{1, 2, 3}), operation: "update", arg: raw([]int{2, 3, 4, 5}), dflt: rawNull,
			want: []int{1, 2, 3, 4, 5},
		},
		{
			name: "update_list_all_new",
			cur:  raw([]int{1}), operation: "update", arg: raw([]int{2, 3}), dflt: rawNull,
			want: []int{1, 2, 3},
		},
		{
			name: "update_dict_merge",
			cur:  raw(map[string]int{"a": 1, "b": 2}),
			operation: "update", arg: raw(map[string]int{"b": 99, "c": 3}), dflt: rawNull,
			want: map[string]int{"a": 1, "b": 99, "c": 3},
		},

		// ---- default when absent (Set-level seeding, not the "default" op) --
		{
			// Simulates: key absent in store → seed from dflt=0; then add 5 → 5
			name: "absent_key_add_uses_dflt_zero",
			cur:  rawNull, operation: "add", arg: raw(5), dflt: raw(0),
			want: 5,
		},
		{
			// Simulates: key absent, dflt=10; then mul 3 → 30
			name: "absent_key_mul_uses_dflt",
			cur:  rawNull, operation: "mul", arg: raw(3), dflt: raw(10),
			want: 30,
		},
	}

	for _, tc := range tests {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			got := applyOperation(tc.cur, tc.operation, tc.arg, tc.dflt)
			wantBytes, err := json.Marshal(tc.want)
			if err != nil {
				t.Fatalf("marshal want: %v", err)
			}
			// Compare as canonical JSON (re-marshal both so formatting is uniform)
			var gotVal, wantVal any
			if err := json.Unmarshal(got, &gotVal); err != nil {
				t.Fatalf("unmarshal got %q: %v", got, err)
			}
			if err := json.Unmarshal(wantBytes, &wantVal); err != nil {
				t.Fatalf("unmarshal want %q: %v", wantBytes, err)
			}
			gotNorm, _ := json.Marshal(gotVal)
			wantNorm, _ := json.Marshal(wantVal)
			if string(gotNorm) != string(wantNorm) {
				t.Errorf("applyOperation(%q, %q, %q, %q)\n  got  %s\n  want %s",
					tc.cur, tc.operation, tc.arg, tc.dflt, gotNorm, wantNorm)
			}
		})
	}
}

// TestApplyOperationChain verifies that repeated add converges to the same total
// as Python for EnergyLink-style accumulation (many setters adding small amounts).
func TestApplyOperationChain(t *testing.T) {
	dflt := raw(0)
	cur := rawNull // start absent

	additions := []int64{1_000_000, 2_000_000, 500_000, 3_000_000, 1_500_000}
	expected := int64(0)
	for _, v := range additions {
		expected += v
	}

	for _, v := range additions {
		cur = applyOperation(cur, "add", raw(v), dflt)
	}

	var got int64
	if err := json.Unmarshal(cur, &got); err != nil {
		t.Fatalf("unmarshal result: %v", err)
	}
	if got != expected {
		t.Errorf("EnergyLink chain: got %d, want %d", got, expected)
	}
}

// TestOriginalValuePreserved verifies the handleSet contract: original_value
// reflects the pre-operation state. We test this through applyOperation directly
// since handleSet reads h.store[key] before mutating it.
func TestOriginalValuePreserved(t *testing.T) {
	// Simulate: store has key="energy" value=100; client does add 50.
	stored := raw(100)
	original := stored // capture before mutation
	newVal := applyOperation(stored, "add", raw(50), raw(0))

	var origInt, newInt int64
	json.Unmarshal(original, &origInt)
	json.Unmarshal(newVal, &newInt)

	if origInt != 100 {
		t.Errorf("original_value: got %d, want 100", origInt)
	}
	if newInt != 150 {
		t.Errorf("new value after add: got %d, want 150", newInt)
	}
}
