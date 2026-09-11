/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

	http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"slices"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func identityTestAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "agent", Namespace: "kubeagents-system"},
	}
}

// The inbox trap, pinned. Push delivery and every JetStream API request come
// back on an inbox subject, each principal is granted only its own prefix, and
// the client sets that prefix from its user name. A principal whose subscribe
// list does not contain its own prefix authenticates, publishes, and then hangs
// on the first reply — found live twice in W6 (the provision Job could never
// succeed; no consumer could ever ack). This asserts the property rather than
// the spelling of any one grant.
func TestEveryPrincipalMaySubscribeToItsOwnInbox(t *testing.T) {
	for _, id := range a2aIdentities(identityTestAgent()) {
		if id.account == a2aAccountSys {
			// $SYS holds no application inbox grants; its user is a
			// human with the system account's own privileges.
			continue
		}
		want := "_INBOX." + id.user + ".>"
		if !slices.Contains(id.subscribe, want) {
			t.Errorf("%s: subscribe list lacks %q; every reply it waits on would time out", id.user, want)
		}
		if !slices.Contains(id.publish, want) {
			t.Errorf("%s: publish list lacks %q; it could not answer its own requests", id.user, want)
		}
	}
}

// No principal may hold another's inbox prefix. Without this the whole
// connect-time property leaks through the reply path: an agent that can
// subscribe to another's inbox reads what that principal's subject grants
// withheld.
func TestNoPrincipalHoldsAnotherPrincipalsInbox(t *testing.T) {
	ids := a2aIdentities(identityTestAgent())
	for _, id := range ids {
		for _, other := range ids {
			if other.user == id.user {
				continue
			}
			foreign := "_INBOX." + other.user + ".>"
			if slices.Contains(id.subscribe, foreign) {
				t.Errorf("%s may subscribe to %s's inbox (%q)", id.user, other.user, foreign)
			}
		}
	}
}

func TestPrincipalsAreDistinct(t *testing.T) {
	seenUser := map[string]bool{}
	seenSA := map[string]bool{}
	for _, id := range a2aIdentities(identityTestAgent()) {
		if seenUser[id.user] {
			t.Errorf("two principals share the NATS user %q", id.user)
		}
		seenUser[id.user] = true

		if id.auth != a2aAuthCallout {
			continue
		}
		// Two ServiceAccounts mapping to one user would re-create the
		// shared credential the callout exists to end, and the callout
		// refuses such a map outright — better to fail here than to
		// render a map the callout will reject at startup.
		if seenSA[id.serviceAccount] {
			t.Errorf("two principals share the ServiceAccount %q", id.serviceAccount)
		}
		seenSA[id.serviceAccount] = true
	}
}

// Each auth mode owes different fields, and a principal carrying the wrong set
// renders into the wrong place: a callout principal with no ServiceAccount
// cannot be resolved, and a static one with no creds key renders a password of
// "" — a user anyone can log in as, which is W6 finding #9 in a new costume.
func TestEachPrincipalCarriesWhatItsAuthModeNeeds(t *testing.T) {
	for _, id := range a2aIdentities(identityTestAgent()) {
		switch id.auth {
		case a2aAuthCallout:
			if id.serviceAccount == "" {
				t.Errorf("%s authenticates by callout but names no ServiceAccount", id.user)
			}
			if !strings.HasPrefix(id.serviceAccount, "system:serviceaccount:") {
				t.Errorf("%s: ServiceAccount %q is not in the form TokenReview reports", id.user, id.serviceAccount)
			}
			if id.credsKey != "" {
				t.Errorf("%s authenticates by callout but also carries the creds key %q; it must have no shared secret at all", id.user, id.credsKey)
			}
		case a2aAuthStatic:
			if id.credsKey == "" {
				t.Errorf("%s authenticates statically but names no creds key; it would render an empty password", id.user)
			}
			if id.serviceAccount != "" {
				t.Errorf("%s authenticates statically but names a ServiceAccount", id.user)
			}
		}
		if id.account == "" {
			t.Errorf("%s names no account", id.user)
		}
	}
}

// Every static principal must be exempted from the callout, and the exemption
// list is built from exactly this set. A static user missing from auth_users is
// refused at connect by a callout that has never heard of it.
func TestStaticAndCalloutPrincipalsPartitionTheSet(t *testing.T) {
	agent := identityTestAgent()
	all := a2aIdentities(agent)
	static := staticIdentities(agent)
	callout := calloutIdentities(agent)

	if len(static)+len(callout) != len(all) {
		t.Fatalf("static (%d) + callout (%d) != all (%d); a principal is in neither render or both",
			len(static), len(callout), len(all))
	}
	for _, id := range static {
		if id.auth != a2aAuthStatic {
			t.Errorf("%s is in the static set with auth mode %v", id.user, id.auth)
		}
	}
	for _, id := range callout {
		if id.auth != a2aAuthCallout {
			t.Errorf("%s is in the callout set with auth mode %v", id.user, id.auth)
		}
	}
}

// The residue, asserted so it cannot grow quietly. Each of these has a reason
// recorded at its definition, and the reasons are not the same kind of thing:
// web can never present a ServiceAccount token because a browser has none;
// worker has no identity to present because session pods are spawned without
// one; sys is a human; gateway could move today but its client program lands
// separately from this render, so moving the identity first would refuse it at
// connect on every install; and seed is applied rather than rendered, so
// dropping its user would break an object already running on installs today. A
// sixth name here means someone added a principal without asking whether it
// could have an identity.
func TestTheStaticResidueIsExactlyTheOnesWithReasons(t *testing.T) {
	var got []string
	for _, id := range staticIdentities(identityTestAgent()) {
		got = append(got, id.user)
	}
	want := []string{"gateway", "worker", "seed", "web", "sys"}
	if !slices.Equal(got, want) {
		t.Errorf("static principals = %v, want %v.\nA new static principal needs a recorded reason it cannot present a ServiceAccount token, and a card that closes it if it can.", got, want)
	}
}

// The other half of the static-residue check, and the one that catches the
// opposite mistake: a principal declared on the callout that no workload can
// present a token for.
//
// An entry in the identity map is a grant on a ServiceAccount, live from the
// moment the map is served. If nothing renders an a2a-bus token for that
// account, the grant is not documentation of a future client — it is a standing
// authorization waiting for one, in the file that is supposed to record who
// actually authenticates. This branch had exactly that: an `agent` principal
// keyed on the platform agent's ServiceAccount, whose only bus client is the
// Hermes bridge sidecar authenticating as static `worker`.
//
// So the set is pinned by name rather than by shape. Adding a principal here
// means saying, at review, which rendered workload presents its token.
func TestEveryCalloutPrincipalHasAClientThatCanPresentAToken(t *testing.T) {
	var got []string
	for _, id := range a2aIdentities(identityTestAgent()) {
		if id.auth == a2aAuthCallout {
			got = append(got, id.user)
		}
	}
	want := []string{"provision"}
	if !slices.Equal(got, want) {
		t.Errorf("callout principals = %v, want %v.\nA new callout principal needs a rendered workload that mounts an a2a-bus token for its ServiceAccount (a2aBusTokenVolumeSource / a2aBusTokenVolumeMount). Without one the entry authorizes nobody and misreports who authenticates.", got, want)
	}
}

// a2aTestCalloutKeys generates a real keypair set for render tests. Real rather
// than a fixture string: the server validates both key types and refuses to
// start on either being wrong, so a test rendering a placeholder would assert
// against a config the server would reject.
func a2aTestCalloutKeys(t *testing.T) *a2aCalloutKeys {
	t.Helper()
	keys, _, err := generateA2ACalloutKeys()
	if err != nil {
		t.Fatalf("generateA2ACalloutKeys: %v", err)
	}
	return keys
}

// TestOnlyTheseIdentitiesHoldTheBareJetStreamAPIGrant pins the sentence in
// renderA2ANATSConf's doc comment that names them.
//
// That sentence used to read "$JS.API.> on every app user is playground
// posture". It was a summary, nothing checked it, and it was wrong in both
// directions by the time anyone read it again: web has carried the enumerated
// per-stream subjects since before the callout existed, so "every" was never
// true, and provision moved to the callout, so the set changed underneath it.
// A comment that names a set is a comment that needs a test naming the same
// set, which is what this is. Seed came off the list the same way, in
// gke-labs#1306: its $JS.API grant is now a2aSeedJetStreamGrants(), CREATE and
// INFO on the streams provisioning names. Worker came off it in #1393, the same
// way: a2aWorkerJetStreamGrants(), INFO/CONSUMER/DIRECT.GET on the four streams
// it touches. Gateway is the last one, and it is the one that cannot narrow on
// this branch's terms -- it has no client presenting a token yet.
//
// Failing here means one of two things and they want opposite responses. An
// identity dropping off the list is the callout doing its job — narrow the
// grant, then narrow this list and the comment with it. An identity appearing
// on it is a new bare $JS.API.>, which is a principal that can create, delete
// or purge any stream and any consumer on the account, including another
// principal's. That is not a list to grow without an argument in the identity's
// own comment for why it cannot be enumerated instead.
func TestOnlyTheseIdentitiesHoldTheBareJetStreamAPIGrant(t *testing.T) {
	const bare = "$JS.API.>"
	expected := []string{"gateway"}

	var got []string
	for _, id := range a2aIdentities(identityTestAgent()) {
		if slices.Contains(id.publish, bare) || slices.Contains(id.subscribe, bare) {
			got = append(got, id.user)
		}
	}
	slices.Sort(got)

	if !slices.Equal(got, expected) {
		t.Errorf("the set of identities holding a bare %s is %v, and renderA2ANATSConf's doc "+
			"comment says %v; correct whichever is wrong, and read this test's own comment "+
			"first because the two directions want opposite fixes", bare, got, expected)
	}
}
