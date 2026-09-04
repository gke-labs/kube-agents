package lib

import (
	"fmt"
	"testing"
	"time"
)

// Eviction must not break the at-most-once property while a redelivery can
// still arrive: entries younger than the retention floor are kept even when
// the set is over capacity.
func TestDedupSet_RetentionFloorBeatsCapacity(t *testing.T) {
	d := newDedupSetWithRetention(2, time.Hour)
	for i := 0; i < 10; i++ {
		if !d.add(fmt.Sprintf("env-%d", i)) {
			t.Fatalf("env-%d falsely deduped", i)
		}
	}
	// All ten are younger than the floor: every duplicate is still caught.
	for i := 0; i < 10; i++ {
		if d.add(fmt.Sprintf("env-%d", i)) {
			t.Fatalf("env-%d duplicate reached the application: evicted under capacity pressure", i)
		}
	}
}

// Once past the retention floor, capacity wins and old entries fall out.
func TestDedupSet_EvictsAgedEntries(t *testing.T) {
	d := newDedupSetWithRetention(2, 0)
	d.add("a")
	d.add("b")
	d.add("c")
	if len(d.seen) > 2 {
		t.Fatalf("set holds %d entries past both capacity and retention", len(d.seen))
	}
}
