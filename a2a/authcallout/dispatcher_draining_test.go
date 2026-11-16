package authcallout

import (
	"os"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// dispatcherDrainingFixturePath is the operator's identity map with profile
// "tse" live and "gone" draining (#5074), written by the operator's tests.
const dispatcherDrainingFixturePath = "testdata/rendered-identity-map-dispatcher-draining.json"

// A draining profile keeps the dispatcher's terminal and stop grants and
// none of its durables, enforced by a real server through this callout: the
// dispatcher can still publish a held task's failure terminal on the
// profile's .supervisor and read its stop records, and can no longer pull or
// ack its submissions.
func TestTheRenderedDispatcherKeepsADrainingProfilesTerminalGrantOnly(t *testing.T) {
	raw, err := os.ReadFile(dispatcherDrainingFixturePath)
	if err != nil {
		t.Fatalf("reading %s: %v\nRegenerate with: go test ./internal/controller/ -run 'Draining' -update (in k8s-operator)", dispatcherDrainingFixturePath, err)
	}
	h := startHarness(t, string(raw), map[string]Attested{
		tokenDispatcher: {ServiceAccount: renderedDispatcherSA},
	})
	nc, violations := h.connectAs(t, "dispatcher", tokenDispatcher)
	gone := lib.ProfileDurableName("gone", "read", "top")
	checkPublish(t, nc, violations, map[string]bool{
		// Kept: a held task's failure terminal, and its stop records.
		lib.TaskSupervisorSubject("gone", "t1"):              false,
		"$JS.API.DIRECT.GET.DISPATCH_STOPS.a2a.stop.gone.t1": false,
		// Gone with the durables: pulling or acking its submissions.
		"$JS.API.CONSUMER.MSG.NEXT." + lib.ProfileSubmitStream + "." + gone: true,
		"$JS.API.CONSUMER.INFO." + lib.ProfileSubmitStream + "." + gone:     true,
		"$JS.ACK." + lib.ProfileSubmitStream + "." + gone + ".1.1.1.1.0":    true,
		// The live profile is untouched.
		lib.TaskSupervisorSubject("tse", "t1"): false,
		"$JS.API.CONSUMER.MSG.NEXT." + lib.ProfileSubmitStream + "." + lib.ProfileDurableName("tse", "read", "top"): false,
		// Nothing wider than the profile's own subjects.
		lib.TaskSupervisorSubject("session-abc", "t1"): true,
		lib.TaskEventsSubject("gone", "t1"):            true,
	})
	if subscribeRefused(t, nc, violations, "a2a.stop.gone.*") {
		t.Errorf("the dispatcher lost its stop subscription for the draining profile")
	}
}
