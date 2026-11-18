package busmigrate

import (
	"context"
	"errors"
	"slices"
	"testing"

	"github.com/nats-io/nats.go"
)

// provisionOrder is the operator's a2aProvisionedStreams, the list every
// clear and restore is handed: the controller package pins that
// KV_PROFILE_LEASES comes before PROFILE_SUBMITS there.
var provisionOrder = []string{
	"TASKS", "DIRECTORY", "TOPICS-STATE", "TOPICS-JOURNAL",
	"KV_runtime-state", "KV_session-state", "KV_cap", "KV_feedback", "KV_people",
	"KV_PROFILE_LEASES", "PROFILE_SUBMITS", "DISPATCH_STOPS",
}

// A clear that fails at any stream never leaves PROFILE_SUBMITS on the bus
// with KV_PROFILE_LEASES gone, and more generally never leaves a stream
// without one listed before it: what is left is always a prefix of the
// provision order.
func TestAClearFailingAnywhereLeavesNoStreamWithoutItsIndex(t *testing.T) {
	defer func(d func(context.Context, *nats.Conn, string) error) { deleteStream = d }(deleteStream)
	for _, failing := range provisionOrder {
		present := map[string]bool{}
		for _, n := range provisionOrder {
			present[n] = true
		}
		deleteStream = func(_ context.Context, _ *nats.Conn, name string) error {
			if name == failing {
				return errors.New("refused")
			}
			delete(present, name)
			return nil
		}
		cleared, err := ClearStreams(context.Background(), nil, provisionOrder)
		if err == nil {
			t.Fatalf("failing %s: the clear succeeded", failing)
		}
		if present["PROFILE_SUBMITS"] && !present["KV_PROFILE_LEASES"] {
			t.Errorf("failing %s: PROFILE_SUBMITS is left without KV_PROFILE_LEASES", failing)
		}
		var left []string
		for _, n := range provisionOrder {
			if present[n] {
				left = append(left, n)
			}
		}
		if !slices.Equal(left, provisionOrder[:len(left)]) {
			t.Errorf("failing %s: left %v, not a prefix of the provision order", failing, left)
		}
		for _, n := range cleared {
			if present[n] {
				t.Errorf("failing %s: %s reported cleared but still there", failing, n)
			}
		}
	}
}
