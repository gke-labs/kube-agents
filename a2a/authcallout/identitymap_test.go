package authcallout

import (
	"strings"
	"testing"
)

const goodMap = `{
  "version": "v1",
  "identities": [
    {
      "serviceAccount": "system:serviceaccount:kubeagents-system:agent-a2a-gateway",
      "user": "gateway",
      "account": "APP",
      "grants": {"publish": ["a2a.tasks.>"], "subscribe": ["_INBOX.gateway.>"]}
    }
  ]
}`

func TestParseIdentityMapAcceptsARenderedMap(t *testing.T) {
	m, err := ParseIdentityMap([]byte(goodMap))
	if err != nil {
		t.Fatalf("ParseIdentityMap: %v", err)
	}
	if m.Version != "v1" {
		t.Errorf("version = %q, want v1", m.Version)
	}
	id, ok := m.Lookup("system:serviceaccount:kubeagents-system:agent-a2a-gateway")
	if !ok {
		t.Fatal("gateway ServiceAccount not found")
	}
	if id.User != "gateway" || id.Account != "APP" {
		t.Errorf("got user=%q account=%q, want gateway/APP", id.User, id.Account)
	}
}

func TestLookupReportsAnUnmappedServiceAccount(t *testing.T) {
	m, err := ParseIdentityMap([]byte(goodMap))
	if err != nil {
		t.Fatalf("ParseIdentityMap: %v", err)
	}
	// The refusal path the DoD asserts against a live server starts here:
	// an identity the cluster will happily vouch for, that this deployment
	// has no entry for, must be distinguishable from a mapped one.
	if _, ok := m.Lookup("system:serviceaccount:kubeagents-system:some-other-sa"); ok {
		t.Error("an unmapped ServiceAccount was found in the map")
	}
}

func TestParseIdentityMapRejectsMapsItCannotServe(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want string
	}{
		{
			name: "no version",
			raw:  `{"identities":[{"serviceAccount":"system:serviceaccount:ns:a","user":"a","account":"APP","grants":{"publish":["x.>"]}}]}`,
			want: "no version",
		},
		{
			name: "username is not a ServiceAccount",
			raw:  `{"version":"v1","identities":[{"serviceAccount":"alice","user":"a","account":"APP","grants":{"publish":["x.>"]}}]}`,
			want: "is not system:serviceaccount:",
		},
		{
			name: "ServiceAccount missing its namespace",
			raw:  `{"version":"v1","identities":[{"serviceAccount":"system:serviceaccount:a","user":"a","account":"APP","grants":{"publish":["x.>"]}}]}`,
			want: "is not system:serviceaccount:",
		},
		{
			name: "no NATS user",
			raw:  `{"version":"v1","identities":[{"serviceAccount":"system:serviceaccount:ns:a","account":"APP","grants":{"publish":["x.>"]}}]}`,
			want: "has no user",
		},
		{
			name: "no account",
			raw:  `{"version":"v1","identities":[{"serviceAccount":"system:serviceaccount:ns:a","user":"a","grants":{"publish":["x.>"]}}]}`,
			want: "has no account",
		},
		{
			name: "no grants at all",
			raw:  `{"version":"v1","identities":[{"serviceAccount":"system:serviceaccount:ns:a","user":"a","account":"APP","grants":{}}]}`,
			want: "has no grants",
		},
		{
			name: "two entries for one ServiceAccount",
			raw: `{"version":"v1","identities":[
			  {"serviceAccount":"system:serviceaccount:ns:a","user":"a","account":"APP","grants":{"publish":["x.>"]}},
			  {"serviceAccount":"system:serviceaccount:ns:a","user":"b","account":"APP","grants":{"publish":["y.>"]}}]}`,
			want: "duplicate serviceAccount",
		},
		{
			name: "two ServiceAccounts sharing a NATS user",
			raw: `{"version":"v1","identities":[
			  {"serviceAccount":"system:serviceaccount:ns:a","user":"a","account":"APP","grants":{"publish":["x.>"]}},
			  {"serviceAccount":"system:serviceaccount:ns:b","user":"a","account":"APP","grants":{"publish":["y.>"]}}]}`,
			want: "duplicate user",
		},
		{
			name: "a field the renderer does not know about",
			raw:  `{"version":"v1","identities":[{"serviceAccount":"system:serviceaccount:ns:a","user":"a","account":"APP","grants":{"publish":["x.>"]},"tier":"admin"}]}`,
			want: "decoding identity map",
		},
		{
			// The fail-closed rule for narrowing. An entry that could
			// hold real grants AND be narrowed is one forgotten code
			// path — or one map edit by someone who did not know the
			// code narrowed it — away from handing every session pod
			// the whole bus, which is the shared `worker` credential
			// under a new name.
			name: "a narrowed entry that also carries grants",
			raw:  `{"version":"v1","identities":[{"serviceAccount":"system:serviceaccount:ns:a","user":"a","account":"APP","narrowing":"pod","grants":{"publish":["a2a.tasks.>"]}}]}`,
			want: "must carry none",
		},
		{
			name: "a narrowed entry carrying only a subscribe",
			raw:  `{"version":"v1","identities":[{"serviceAccount":"system:serviceaccount:ns:a","user":"a","account":"APP","narrowing":"pod","grants":{"subscribe":["a2a.tasks.>"]}}]}`,
			want: "must carry none",
		},
		{
			// A narrowing this build does not implement must not fall
			// through to the entry's own (empty) grants, and must not
			// be ignored. Refusing at parse keeps a map rendered by a
			// newer operator from being served by an older callout.
			name: "a narrowing this callout does not implement",
			raw:  `{"version":"v1","identities":[{"serviceAccount":"system:serviceaccount:ns:a","user":"a","account":"APP","narrowing":"node"}]}`,
			want: "does not implement",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := ParseIdentityMap([]byte(tc.raw))
			if err == nil {
				t.Fatalf("map was accepted; want refusal containing %q", tc.want)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("error = %v, want it to contain %q", err, tc.want)
			}
		})
	}
}

func TestUsersReportsWhatTheMapServes(t *testing.T) {
	raw := `{"version":"v1","identities":[
	  {"serviceAccount":"system:serviceaccount:ns:z","user":"zed","account":"APP","grants":{"publish":["x.>"]}},
	  {"serviceAccount":"system:serviceaccount:ns:a","user":"alpha","account":"APP","grants":{"publish":["y.>"]}}]}`
	m, err := ParseIdentityMap([]byte(raw))
	if err != nil {
		t.Fatalf("ParseIdentityMap: %v", err)
	}
	got := m.Users()
	want := []string{"alpha", "zed"}
	if len(got) != len(want) || got[0] != want[0] || got[1] != want[1] {
		t.Errorf("Users() = %v, want %v", got, want)
	}
}

// The other half of the fail-closed rule: a narrowed entry with no grants is
// legal, and must not trip the "has no grants" refusal that every other entry
// is held to. Without this the operator cannot render the session principal at
// all, and the fail-closed shape above would be unreachable.
func TestParseIdentityMapAcceptsANarrowedEntryWithNoGrants(t *testing.T) {
	raw := `{"version":"v1","identities":[
		{"serviceAccount":"system:serviceaccount:ns:gw","user":"gateway","account":"APP","grants":{"publish":["a2a.tasks.>"]}},
		{"serviceAccount":"system:serviceaccount:ns:session","user":"session","account":"APP","narrowing":"pod","grants":{"publish":[],"subscribe":[]}}
	]}`
	m, err := ParseIdentityMap([]byte(raw))
	if err != nil {
		t.Fatalf("ParseIdentityMap refused a narrowed entry with no grants: %v", err)
	}
	id, ok := m.Lookup("system:serviceaccount:ns:session")
	if !ok {
		t.Fatal("the narrowed entry is not in the parsed map")
	}
	if id.Narrowing != NarrowingPod {
		t.Errorf("narrowing = %q, want %q", id.Narrowing, NarrowingPod)
	}
}

// An entry with the grants field absent entirely, which is what the operator
// renders when it emits no grant lists at all.
func TestParseIdentityMapAcceptsANarrowedEntryWithNoGrantsField(t *testing.T) {
	raw := `{"version":"v1","identities":[
		{"serviceAccount":"system:serviceaccount:ns:session","user":"session","account":"APP","narrowing":"pod"}
	]}`
	if _, err := ParseIdentityMap([]byte(raw)); err != nil {
		t.Fatalf("ParseIdentityMap: %v", err)
	}
}
