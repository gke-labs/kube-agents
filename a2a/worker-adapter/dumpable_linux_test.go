//go:build linux

package workeradapter

import "testing"

// After HideFromSameUID the process reads as not dumpable, so its /proc
// memory and fd entries are closed to the agent's processes.
func TestHideFromSameUIDMakesTheProcessNotDumpable(t *testing.T) {
	if err := HideFromSameUID(); err != nil {
		t.Fatal(err)
	}
	d, err := dumpable()
	if err != nil || d {
		t.Fatalf("dumpable = %v (%v), want false", d, err)
	}
}
