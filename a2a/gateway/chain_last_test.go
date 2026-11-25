package gateway

import "testing"

// A profile task that delegated is its own chain's end, as observedAs has
// it: chainLast doesn't step into its children, whether one child or a
// batch, so a status read or a stop that names it reads or stops it.
func TestChainLastStopsAtAProfileTask(t *testing.T) {
	for _, tc := range []struct {
		name string
		kids []TaskRef
	}{
		{"one child", []TaskRef{{ID: "c1", Role: taskRoleChild, ParentTaskID: "p", Tier: "read"}}},
		{"a batch", []TaskRef{
			{ID: "c1", Role: taskRoleChild, ParentTaskID: "p", Tier: "read", BatchIndex: 1},
			{ID: "c2", Role: taskRoleChild, ParentTaskID: "p", Tier: "read", BatchIndex: 2},
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			parent := TaskRef{ID: "p", Tier: "unattended", Addressee: "fleet-audit"}
			for _, k := range tc.kids {
				parent.Children = append(parent.Children, k.ID)
			}
			if len(tc.kids) > 1 {
				parent.BatchSize = len(tc.kids)
			}
			rec := &SessionRecord{Key: "trigger:schedule/x", Tasks: append([]TaskRef{parent}, tc.kids...)}
			if last, ok := rec.chainLast(parent); ok {
				t.Fatalf("chainLast(profile parent) = %s, true; want false (the parent is its own end)", last.ID)
			}
			// A session turn above the same shape still steps down.
			sess := TaskRef{ID: "s", Children: []string{"c1"}}
			rec2 := &SessionRecord{Key: "console:x", Tasks: []TaskRef{sess, {ID: "c1", Role: taskRoleChild, ParentTaskID: "s", Tier: "read"}}}
			if last, ok := rec2.chainLast(sess); !ok || last.ID != "c1" {
				t.Fatalf("chainLast(session turn) = %s, %v; want c1, true", last.ID, ok)
			}
		})
	}
}
