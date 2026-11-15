package gateway

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// startInjectProfileRig is the inject rig with the profile rig's install: a
// scope file admitting project p1, the namespace, a generic cluster profile
// that yields to pinned ones, and prod-east pinned to p1/us-east1/prod.
func startInjectProfileRig(t *testing.T) *injectRig {
	t.Helper()
	scope := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scope, []byte(`{"present":true,"projects":["p1"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	r := startInjectRigWith(t, func(c *Config) { c.ScopeFile = scope; c.Namespace = "kubeagents-system" })
	r.g.profiles.put("cluster", &lib.AgentCard{Name: "cluster", Parameters: clusterParams, YieldToPinned: true})
	r.g.profiles.put("prod-east", &lib.AgentCard{Name: "prod-east", Parameters: clusterParams,
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "prod"}})
	return r
}

// postInject POSTs req to the door and returns the status and, on 200, the
// decoded reply.
func (r *injectRig) postInject(t *testing.T, req injectRequest) (int, injectResponse) {
	t.Helper()
	body, err := json.Marshal(req)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := r.do(t, http.MethodPost, r.base+injectPath, body, injectTestToken)
	if err != nil {
		t.Fatalf("POST %s: %v", injectPath, err)
	}
	defer resp.Body.Close()
	var out injectResponse
	if resp.StatusCode == http.StatusOK {
		if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
			t.Fatalf("decoding the inject reply: %v", err)
		}
	}
	return resp.StatusCode, out
}

// A turn the inject door addresses to a profile takes the shared profile
// path: resolved (the generic cluster profile yields to the one pinned to the
// target), checked, and submitted for that profile with its parameters, as
// the console's would be.
func TestAnInjectTurnAddressedToAProfileIsSubmittedForTheResolvedProfile(t *testing.T) {
	t.Parallel()
	r := startInjectProfileRig(t)
	status, out := r.postInject(t, injectRequest{Conversation: "eval-prof-1", Author: injectTestAuthor, Text: "what's failing in kube-system",
		Profile: "cluster", Parameters: targetParams("p1", "us-east1", "prod")})
	if status != http.StatusOK {
		t.Fatalf("status %d, want 200", status)
	}
	envs := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"prod-east.>")
	if len(envs) != 1 {
		t.Fatalf("%d submission(s) for prod-east, want 1", len(envs))
	}
	if out.TaskID == "" || envs[0].env.TaskID != out.TaskID {
		t.Fatalf("reply task %q, submission task %q", out.TaskID, envs[0].env.TaskID)
	}
	params, found, err := lib.TaskParametersOf(messageParts(t, envs[0].env))
	if err != nil || !found || !mapsEqual(params.Parameters, targetParams("p1", "us-east1", "prod")) {
		t.Fatalf("submission parameters %v (found=%v err=%v)", params.Parameters, found, err)
	}
	if envs := streamEnvelopes(t, r.url, lib.TaskInSubject("platform", out.TaskID)); len(envs) != 0 {
		t.Fatalf("a profile turn also reached platform (%d envelope(s))", len(envs))
	}
}

// The door holds the two fields to their shapes before anything is handed
// over: a profile that can't be a subject token, and parameters with no
// profile (platform takes none), are each a 400 with nothing submitted.
func TestTheInjectDoorRefusesAMalformedProfileOrParametersWithoutOne(t *testing.T) {
	t.Parallel()
	r := startInjectProfileRig(t)
	for name, req := range map[string]injectRequest{
		"profile not a subject token": {Conversation: "eval-bad-1", Author: injectTestAuthor, Text: "x", Profile: "cluster.>"},
		"parameters without a profile": {Conversation: "eval-bad-2", Author: injectTestAuthor, Text: "x",
			Parameters: targetParams("p1", "us-east1", "prod")},
	} {
		if status, _ := r.postInject(t, req); status != http.StatusBadRequest {
			t.Errorf("%s: status %d, want 400", name, status)
		}
	}
	if envs := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+">"); len(envs) != 0 {
		t.Fatalf("%d submission(s) after refused POSTs", len(envs))
	}
}

// A profile turn whose parameters the profile doesn't declare is refused by
// the shared path (resolveProfile), not the door: the POST is answered, and
// nothing is submitted.
func TestAnInjectProfileTurnWithAnUndeclaredParameterSubmitsNothing(t *testing.T) {
	t.Parallel()
	r := startInjectProfileRig(t)
	params := targetParams("p1", "us-east1", "dev")
	params["zone"] = "b"
	status, out := r.postInject(t, injectRequest{Conversation: "eval-undeclared", Author: injectTestAuthor, Text: "x",
		Profile: "cluster", Parameters: params})
	if status != http.StatusOK {
		t.Fatalf("status %d, want 200 (the refusal is the room's)", status)
	}
	if out.TaskID != "" {
		t.Fatalf("a task %s started for a refused profile turn", out.TaskID)
	}
	if envs := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+">"); len(envs) != 0 {
		t.Fatalf("%d submission(s) for a refused profile turn", len(envs))
	}
}
