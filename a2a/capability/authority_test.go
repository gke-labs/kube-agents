package capability

import (
	"encoding/json"
	"testing"
)

// TestNoCapabilityAtAllIsThreeShapesNotOne pins the question RefFromAuthority
// asks. "The block carries no capability" has three JSON spellings and only
// one of them is a nil Grants, so a nil check answers it for a third of the
// input.
//
// What it costs to get wrong is not a hole -- every shape here is refused
// under the default -- but a wrong name and a wasted round trip: an executor
// that believes a zero Ref is present spends a verifier call on it and then
// rejects the task as "the capability does not authorize this caller", which
// sends whoever reads the log looking for a permission problem that is not
// there. In the CapabilityOptional rollout window it is a behaviour
// difference: these shapes must run, exactly as `"grants": null` does.
func TestNoCapabilityAtAllIsThreeShapesNotOne(t *testing.T) {
	for _, tc := range []struct {
		name        string
		raw         string
		wantPresent bool
		wantKey     string
		wantErr     bool
	}{
		{name: "no authority block at all", raw: "", wantPresent: false},
		{name: "authority is null", raw: `null`, wantPresent: false},
		{name: "grants is null, the pre-arming gateway", raw: `{"grants":null}`, wantPresent: false},
		{name: "no grants key", raw: `{}`, wantPresent: false},
		{name: "grants is an empty object", raw: `{"grants":{}}`, wantPresent: false},
		{name: "grants.capability is null", raw: `{"grants":{"capability":null}}`, wantPresent: false},
		{name: "grants.capability has an empty key", raw: `{"grants":{"capability":{"key":"","revision":7}}}`, wantPresent: false},
		{
			name:        "a real reference",
			raw:         `{"grants":{"capability":{"key":"cap.abc","revision":7}}}`,
			wantPresent: true,
			wantKey:     "cap.abc",
		},
		{
			// An unpinned reference is still a reference. It has its
			// own refusal and must not be laundered into "absent",
			// which under CapabilityOptional would let it run.
			name:        "a reference with no revision is present, not absent",
			raw:         `{"grants":{"capability":{"key":"cap.abc"}}}`,
			wantPresent: true,
			wantKey:     "cap.abc",
		},
		{name: "a block that does not parse", raw: `{"grants":`, wantErr: true},
		{name: "grants is not an object", raw: `{"grants":"nope"}`, wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var raw json.RawMessage
			if tc.raw != "" {
				raw = json.RawMessage(tc.raw)
			}
			ref, present, err := RefFromAuthority(raw)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("RefFromAuthority(%q) = (%v, %v, nil), want an error", tc.raw, ref, present)
				}
				if present {
					t.Errorf("RefFromAuthority(%q) reported present alongside an error", tc.raw)
				}
				return
			}
			if err != nil {
				t.Fatalf("RefFromAuthority(%q): %v", tc.raw, err)
			}
			if present != tc.wantPresent {
				t.Errorf("RefFromAuthority(%q) present = %v, want %v", tc.raw, present, tc.wantPresent)
			}
			if ref.Key != tc.wantKey {
				t.Errorf("RefFromAuthority(%q) key = %q, want %q", tc.raw, ref.Key, tc.wantKey)
			}
			if !present && ref != (Ref{}) {
				t.Errorf("RefFromAuthority(%q) reported absent but returned %v; an absent reference must be zero", tc.raw, ref)
			}
		})
	}
}

// TestScopeValidateIsExportedForTheExecutorsThatTakeAScopeFromTheEnvironment:
// the gateway validates its ceiling through Entry.Validate, and the two
// executables that take A2A_AUTHORITY_SCOPE directly have no Entry to run. If
// this stops being exported they go back to accepting a scope that refuses
// every task at run time instead of failing at boot.
func TestScopeValidateIsExportedForTheExecutorsThatTakeAScopeFromTheEnvironment(t *testing.T) {
	for _, tc := range []struct {
		scope Scope
		ok    bool
	}{
		{scope: "namespace/kubeagents-system", ok: true},
		{scope: "namespace/kubeagents-system/task/t-1", ok: true},
		{scope: "kubeagents-system"},
		{scope: "namespace/"},
		{scope: "/kubeagents-system"},
		{scope: ""},
	} {
		err := tc.scope.Validate()
		if tc.ok && err != nil {
			t.Errorf("Scope(%q).Validate() = %v, want nil", tc.scope, err)
		}
		if !tc.ok && err == nil {
			t.Errorf("Scope(%q).Validate() = nil, want a refusal", tc.scope)
		}
	}
}
