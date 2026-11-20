package authcallout

import (
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A session pod named "p" would hold its grants under the profile supervisor
// marker (#5301), so it's refused; an ordinary name, or one that only starts
// with p, is not.
func TestASessionPodNamedLikeTheProfileMarkerIsRefused(t *testing.T) {
	if err := validSessionName(lib.TaskProfileSupervisorToken); err == nil {
		t.Errorf("a pod named %q was accepted as a session addressee", lib.TaskProfileSupervisorToken)
	}
	for _, ok := range []string{"cto-otter-1a2b", "p-otter-1a2b", "pp"} {
		if err := validSessionName(ok); err != nil {
			t.Errorf("pod %q refused: %v", ok, err)
		}
	}
}
