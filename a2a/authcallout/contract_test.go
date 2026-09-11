package authcallout

import (
	"os"
	"testing"
)

// The other half of the cross-module contract.
//
// The operator renders the identity map and this package parses it, and the two
// live in Go modules that cannot import each other — so the shape of the JSON
// is the only thing holding them together, and nothing about a rename on one
// side would fail to compile on the other. The operator's test writes this
// fixture from its real render
// (k8s-operator/internal/controller/platformagent_a2a_authmap_test.go,
// regenerate with -update); this one parses it with the production parser,
// unknown fields refused.
//
// So the failure a renamed field produces is: the operator's own test rewrites
// the fixture, and this test fails on the next run with a decode error naming
// the field. That is the whole point — the alternative is a callout that starts,
// refuses the map at runtime, and takes the fabric dark to new connections.
const fixturePath = "testdata/rendered-identity-map.json"

func TestTheOperatorsRenderedMapParses(t *testing.T) {
	raw, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatalf("reading the operator's rendered map: %v", err)
	}

	m, err := ParseIdentityMap(raw)
	if err != nil {
		t.Fatalf("the operator's rendered map does not parse: %v\n"+
			"The two modules' view of the identity map has diverged. The operator's shape is in\n"+
			"k8s-operator/internal/controller/platformagent_a2a_authmap.go; this package's is in identitymap.go.", err)
	}

	if m.Version == "" {
		t.Error("the rendered map carries no version; BusCredentialsReady has nothing to compare")
	}
	if len(m.Identities) == 0 {
		t.Fatal("the rendered map serves no identities")
	}

	// The principals the operator renders for the callout, as of A1. This is
	// a deliberate duplicate of the operator's list: if the two disagree,
	// one of them changed without the other being considered, and for this
	// particular list that means a workload either lost its grants or
	// silently gained some.
	//
	// One name, because the provisioning Job is the only workload the
	// operator renders an a2a-bus token for. The platform agent pod is not
	// here: its only bus client is the Hermes bridge sidecar, which
	// authenticates as static `worker`, so a principal keyed on the agent
	// ServiceAccount would be a grant nothing can present.
	want := map[string]bool{"provision": true}
	for _, id := range m.Identities {
		if !want[id.User] {
			t.Errorf("the operator renders a principal this package did not expect: %q", id.User)
		}
		delete(want, id.User)
	}
	for user := range want {
		t.Errorf("the operator no longer renders the principal %q", user)
	}

	// The static residue must not be here. A principal served by both the
	// callout and nats.conf's auth_users exemption is authenticated by
	// whichever path the client happened to take.
	//
	// gateway is on this list rather than the one above on purpose: it has a
	// ServiceAccount and will move, but its client program lands separately
	// from the render, so the identity may not move before the program that
	// presents a token for it.
	for _, id := range m.Identities {
		switch id.User {
		case "gateway", "worker", "seed", "web", "sys":
			t.Errorf("%q is a static principal and must not appear in the callout's map", id.User)
		}
	}
}

// Every principal the operator ships must be resolvable by the key the callout
// actually looks up — the TokenReview username — and must carry the inbox grant
// its client will set a prefix for.
func TestEveryRenderedPrincipalIsUsable(t *testing.T) {
	raw, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatalf("reading the operator's rendered map: %v", err)
	}
	m, err := ParseIdentityMap(raw)
	if err != nil {
		t.Fatalf("ParseIdentityMap: %v", err)
	}

	for _, id := range m.Identities {
		found, ok := m.Lookup(id.ServiceAccount)
		if !ok {
			t.Errorf("%q does not resolve by its own ServiceAccount %q", id.User, id.ServiceAccount)
			continue
		}
		if found.User != id.User {
			t.Errorf("%q resolves to %q", id.ServiceAccount, found.User)
		}

		inbox := "_INBOX." + id.User + ".>"
		if !contains(id.Grants.Subscribe, inbox) {
			t.Errorf("%q cannot subscribe to %s; every reply it waits on would time out", id.User, inbox)
		}
	}
}

func contains(list []string, want string) bool {
	for _, s := range list {
		if s == want {
			return true
		}
	}
	return false
}
