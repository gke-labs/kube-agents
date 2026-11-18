package busmigrate

import (
	"context"
	"errors"
	"slices"
	"testing"

	"github.com/nats-io/nats.go"
)

// provisionOrder is the operator's migration order (a2aBusMigrationStreams),
// the list every clear and restore is handed: the controller package pins
// that both indexes of PROFILE_SUBMITS come before it there.
var provisionOrder = []string{
	"TASKS", "DIRECTORY", "TOPICS-STATE", "TOPICS-JOURNAL",
	"KV_runtime-state", "KV_session-state", "KV_cap", "KV_feedback", "KV_people",
	"KV_PROFILE_LEASES", "DISPATCH_STOPS", "PROFILE_SUBMITS",
}

// A clear that fails at any stream never leaves PROFILE_SUBMITS on the bus
// with either of its indexes, KV_PROFILE_LEASES or DISPATCH_STOPS, gone:
// what is left is always a prefix of the migration order.
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
		for _, index := range []string{"KV_PROFILE_LEASES", "DISPATCH_STOPS"} {
			if present["PROFILE_SUBMITS"] && !present[index] {
				t.Errorf("failing %s: PROFILE_SUBMITS is left without %s", failing, index)
			}
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

// A stream already gone counts as cleared, so a retry after a partial clear
// continues past the streams it deleted.
func TestAClearCountsAStreamAlreadyGoneAsCleared(t *testing.T) {
	defer func(d func(context.Context, *nats.Conn, string) error) { deleteStream = d }(deleteStream)
	deleteStream = func(_ context.Context, _ *nats.Conn, name string) error {
		if name == "PROFILE_SUBMITS" {
			return &jsAPIError{subject: "$JS.API.STREAM.DELETE." + name, description: "stream not found", code: jsStreamNotFound}
		}
		return nil
	}
	cleared, err := ClearStreams(context.Background(), nil, provisionOrder)
	if err != nil || len(cleared) != len(provisionOrder) {
		t.Fatalf("ClearStreams = %v, %v; want every stream cleared, the one already gone included", cleared, err)
	}
}
