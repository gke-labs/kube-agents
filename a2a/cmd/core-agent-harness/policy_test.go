package main

import (
	"reflect"
	"testing"

	"github.com/go-steer/core-agent/v3/pkg/tools"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// Each grantable built-in turns on exactly its own switch, an empty policy
// offers nothing, and a name outside the worker list is refused.
func TestThePolicyFloorOffersExactlyTheNamedTools(t *testing.T) {
	b, _, _, err := policySetup(coreagentwire.Policy{}, t.TempDir())
	if err != nil || b != (tools.BuiltinTools{}) {
		t.Fatalf("an empty policy offered %+v (err %v)", b, err)
	}
	for _, name := range []string{"bash", "read_file", "read_many_files", "write_file", "edit_file", "list_dir", "glob", "grep"} {
		b, _, _, err := policySetup(coreagentwire.Policy{Tools: []string{name}}, t.TempDir())
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		on := 0
		v := reflect.ValueOf(b)
		for i := 0; i < v.NumField(); i++ {
			if v.Field(i).Kind() == reflect.Bool && v.Field(i).Bool() {
				on++
			}
		}
		probe := b
		if err := probe.Disable(name); err != nil || on != 1 || probe != (tools.BuiltinTools{}) {
			t.Errorf("%s turned on %d switches, or not its own: %+v", name, on, b)
		}
	}
	for _, name := range []string{"fetch_url", "spawn_agent", "delete_file", "nonsense"} {
		if _, _, _, err := policySetup(coreagentwire.Policy{Tools: []string{name}}, t.TempDir()); err == nil {
			t.Errorf("%s was offered", name)
		}
	}
}
